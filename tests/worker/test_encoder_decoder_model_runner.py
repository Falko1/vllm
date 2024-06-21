from typing import List, Optional

import pytest
import torch

from vllm.engine.arg_utils import EngineArgs
from vllm.model_executor.sampling_metadata import SamplingMetadata
from vllm.sequence import SamplingParams, SequenceData, SequenceGroupMetadata
from vllm.worker.enc_dec_model_runner import EncoderDecoderModelRunner
from tests.kernels.utils import (override_backend_env_variable,
                                 STR_XFORMERS_ATTN_VAL)

# Backends under test
#
# Currently only XFormers is supported
BACKEND_NAMES = [STR_XFORMERS_ATTN_VAL]

# CUDA graph scenarios to test
#
# Currently CUDA graph is not supported
ENFORCE_EAGER = [True]


def _create_model_runner(model: str, *args,
                         **kwargs) -> EncoderDecoderModelRunner:
    engine_args = EngineArgs(model, *args, **kwargs)
    engine_config = engine_args.create_engine_config()
    model_runner = EncoderDecoderModelRunner(
        model_config=engine_config.model_config,
        parallel_config=engine_config.parallel_config,
        scheduler_config=engine_config.scheduler_config,
        device_config=engine_config.device_config,
        cache_config=engine_config.cache_config,
        load_config=engine_config.load_config,
        lora_config=engine_config.lora_config,
        is_driver_worker=True,
    )
    return model_runner


@pytest.mark.parametrize("batch_size", list(range(1, 257)))
@pytest.mark.parametrize("backend_name", BACKEND_NAMES)
@pytest.mark.parametrize("enforce_eager", ENFORCE_EAGER)
def test_prepare_prompt(batch_size, backend_name, enforce_eager, monkeypatch):

    # Force Attention wrapper backend
    override_backend_env_variable(monkeypatch, backend_name)

    model_runner = _create_model_runner(
        "facebook/bart-base",
        max_num_batched_tokens=100000,
        max_num_seqs=100000,
        enable_chunked_prefill=False,
        enforce_eager=enforce_eager
    )

    seq_lens: List[int] = []
    encoder_seq_lens: List[int] = []
    seq_group_metadata_list: List[SequenceGroupMetadata] = []
    block_tables = {0: [1]}
    cross_block_table = [2]
    for i in range(batch_size):
        # make sure all tokens fit into one block
        seq_len = i % (model_runner.block_size - 1) + 1
        seq_lens.append(seq_len)
        seq_data = SequenceData(list(range(seq_len)))
        encoder_seq_len = (i + 1) % (model_runner.block_size - 1) + 1
        encoder_seq_lens.append(encoder_seq_len)
        encoder_seq_data = SequenceData(list(range(encoder_seq_len)))
        seq_group_metadata = SequenceGroupMetadata(
            request_id=f"test_{i}",
            is_prompt=True,
            seq_data={0: seq_data},
            sampling_params=SamplingParams(temperature=0),
            block_tables=block_tables,
            encoder_seq_data=encoder_seq_data,
            cross_block_table=cross_block_table)
        assert seq_group_metadata.token_chunk_size == seq_data.get_len()
        seq_group_metadata_list.append(seq_group_metadata)

    expected_selected_token_indices = []
    selected_token_start_idx = 0
    for seq_len in seq_lens:
        expected_selected_token_indices.append(selected_token_start_idx +
                                               seq_len - 1)
        selected_token_start_idx += seq_len

    # Decoder model input
    model_input = model_runner._prepare_model_input(seq_group_metadata_list)
    input_tokens = model_input.input_tokens
    input_positions = model_input.input_positions
    attn_metadata = model_input.attn_metadata
    return_seq_lens = model_input.seq_lens
    slot_mapping = model_input.slot_mapping
    assert return_seq_lens == seq_lens
    assert len(slot_mapping) == len(input_tokens)

    # Encoder model input
    encoder_model_input = model_runner._prepare_encoder_model_input(
        seq_group_metadata_list, attn_metadata)
    encoder_input_tokens = encoder_model_input.input_tokens
    encoder_input_positions = encoder_model_input.input_positions
    cross_slot_mapping = attn_metadata.cross_slot_mapping
    assert len(cross_slot_mapping) == len(encoder_input_tokens)

    # Verify input metadata is correct for prompts.
    # - Decoder attention metadata
    device = model_runner.device
    assert attn_metadata.num_prefills > 0
    assert attn_metadata.num_decode_tokens == 0
    assert torch.allclose(
        attn_metadata.seq_lens_tensor,
        torch.tensor(seq_lens, device=device, dtype=torch.int))
    assert attn_metadata.seq_lens == seq_lens
    assert attn_metadata.max_prefill_seq_len == max(seq_lens)
    assert attn_metadata.max_decode_seq_len == 0
    # - Encoder attention metadata
    assert attn_metadata.encoder_seq_lens == encoder_seq_lens
    assert torch.allclose(
        attn_metadata.encoder_seq_lens_tensor,
        torch.tensor(encoder_seq_lens, device=device, dtype=torch.int))
    assert attn_metadata.max_encoder_seq_len == max(encoder_seq_lens)
    assert attn_metadata.num_encoder_tokens == sum(encoder_seq_lens)

    # Test decoder subquery start locs.
    start_idx = 0
    start_loc = [start_idx]
    for seq_len in seq_lens:
        start_idx += seq_len
        start_loc.append(start_idx)
    assert torch.allclose(
        attn_metadata.query_start_loc,
        torch.tensor(start_loc, dtype=torch.int32, device=device))

    # Test decoder seq start locs. Note that for normal prefill it is
    # equivalent to query_start_loc.
    start_idx = 0
    seq_start_loc = [start_idx]
    for seq_len in seq_lens:
        start_idx += seq_len
        seq_start_loc.append(start_idx)

    assert torch.allclose(
        attn_metadata.seq_start_loc,
        torch.tensor(start_loc, dtype=torch.int32, device=device))
    assert torch.allclose(
        attn_metadata.context_lens_tensor,
        torch.zeros(attn_metadata.context_lens_tensor.shape[0],
                    dtype=torch.int,
                    device=device))

    # Verify block tables are correct for prompts
    # - Decoder self-attention
    expected = torch.tensor([[] for _ in range(len(seq_group_metadata_list))],
                            dtype=torch.int32,
                            device=model_runner.device)
    assert torch.allclose(attn_metadata.block_tables, expected)
    # - Encoder/decoder cross-attention
    # expected = torch.tensor([[] for _ in range(len(seq_group_metadata_list))],
    #                         dtype=torch.int32,
    #                         device=model_runner.device)
    assert torch.allclose(attn_metadata.cross_block_tables, expected)

    # Cuda graph should not be used for prefill, regardless of
    # enforce_eager setting
    assert attn_metadata.use_cuda_graph is False

    # Verify the lengths of input tokens & positions
    # - Decoder
    assert len(input_tokens) == sum(seq_lens)
    assert len(input_positions) == sum(seq_lens)
    torch.testing.assert_close(input_tokens, 
                               input_positions)
    # - Encoder
    assert len(encoder_input_tokens) == sum(encoder_seq_lens)
    assert len(encoder_input_tokens) == sum(encoder_seq_lens)
    torch.testing.assert_close(encoder_input_tokens, 
                               encoder_input_positions)

    sampling_metadata = SamplingMetadata.prepare(
        seq_group_metadata_list,
        seq_lens,
        query_lens=seq_lens,
        device=model_runner.device,
        pin_memory=model_runner.pin_memory)
    actual = sampling_metadata.selected_token_indices
    expected = torch.tensor(expected_selected_token_indices,
                            device=actual.device,
                            dtype=actual.dtype)
    torch.testing.assert_close(actual, expected)
    torch.allclose(input_tokens, input_positions)

    actual = sampling_metadata.selected_token_indices
    expected = torch.tensor(expected_selected_token_indices,
                            device=actual.device,
                            dtype=actual.dtype)
    torch.testing.assert_close(actual, expected)

@pytest.mark.parametrize("batch_size", list(range(1, 257)))
@pytest.mark.parametrize("backend_name", BACKEND_NAMES)
@pytest.mark.parametrize("enforce_eager", ENFORCE_EAGER)
def test_prepare_decode(batch_size, backend_name, enforce_eager, monkeypatch):

    # Force Attention wrapper backend
    override_backend_env_variable(monkeypatch, backend_name)

    model_runner = _create_model_runner(
        "facebook/bart-base",
        max_num_batched_tokens=100000,
        max_num_seqs=100000,
        enable_chunked_prefill=False,
        enforce_eager=enforce_eager
    )

    seq_lens: List[int] = []
    encoder_seq_lens: List[int] = []
    seq_group_metadata_list: List[SequenceGroupMetadata] = []
    block_tables = {0: [1]}
    cross_block_table = [2]
    for i in range(batch_size):
        # make sure all tokens fit into one block
        seq_len = i % (model_runner.block_size - 1) + 1
        seq_lens.append(seq_len)
        seq_data = SequenceData(list(range(seq_len)))
        encoder_seq_len = (i + 1) % (model_runner.block_size - 1) + 1
        encoder_seq_lens.append(encoder_seq_len)
        encoder_seq_data = SequenceData(list(range(encoder_seq_len)))
        seq_group_metadata = SequenceGroupMetadata(
            request_id=f"test_{i}",
            is_prompt=False,
            seq_data={0: seq_data},
            sampling_params=SamplingParams(temperature=0),
            block_tables=block_tables,
            encoder_seq_data=encoder_seq_data,
            cross_block_table=cross_block_table)
        assert seq_group_metadata.token_chunk_size == 1
        seq_group_metadata_list.append(seq_group_metadata)

    # Decoder model input
    model_input = model_runner._prepare_model_input(seq_group_metadata_list)
    input_tokens = model_input.input_tokens
    input_positions = model_input.input_positions
    attn_metadata = model_input.attn_metadata
    return_seq_lens = model_input.seq_lens
    slot_mapping = model_input.slot_mapping
    assert return_seq_lens == seq_lens
    assert len(slot_mapping) == len(input_tokens)

    # Encoder model input
    encoder_model_input = model_runner._prepare_encoder_model_input(
        seq_group_metadata_list, attn_metadata)
    encoder_input_tokens = encoder_model_input.input_tokens
    encoder_input_positions = encoder_model_input.input_positions
    return_encoder_seq_lens = attn_metadata.encoder_seq_lens
    cross_slot_mapping = attn_metadata.cross_slot_mapping
    assert return_encoder_seq_lens == encoder_seq_lens
    assert len(cross_slot_mapping) == len(encoder_input_tokens)

    # Verify input metadata is correct for decode phase.
    # - Decoder attention metadata
    device = model_runner.device
    assert attn_metadata.num_prefills == 0
    assert attn_metadata.num_decode_tokens > 0
    assert torch.allclose(
        attn_metadata.seq_lens_tensor,
        torch.tensor(seq_lens, device=device, dtype=torch.int))
    assert attn_metadata.seq_lens == seq_lens
    assert attn_metadata.max_prefill_seq_len == 0
    assert attn_metadata.max_decode_seq_len == max(seq_lens)
    # - Encoder attention metadata
    assert attn_metadata.encoder_seq_lens == encoder_seq_lens
    assert torch.allclose(
        attn_metadata.encoder_seq_lens_tensor,
        torch.tensor(encoder_seq_lens, device=device, dtype=torch.int))
    assert attn_metadata.max_encoder_seq_len == max(encoder_seq_lens)
    assert attn_metadata.num_encoder_tokens == sum(encoder_seq_lens)

    # Test decoder subquery start locs.
    start_idx = 0
    start_loc = [start_idx]
    for seq_len in seq_lens:
        # 1 decoded token per sequence
        start_idx += 1
        start_loc.append(start_idx)
    assert torch.allclose(
        attn_metadata.query_start_loc,
        torch.tensor(start_loc, dtype=torch.int32, device=device))

    # Test decoder seq start locs. Note that for normal prefill it is
    # equivalent to query_start_loc.
    start_idx = 0
    seq_start_loc = [start_idx]
    for seq_len in seq_lens:
        start_idx += seq_len
        seq_start_loc.append(start_idx)

    assert torch.allclose(
        attn_metadata.seq_start_loc,
        torch.tensor(seq_start_loc, dtype=torch.int32, device=device))
    assert torch.allclose(
        attn_metadata.context_lens_tensor,
        torch.tensor([seq_len-1 for seq_len in seq_lens],
                    dtype=torch.int,
                    device=device))

    # Verify block tables are correct for prompts
    # - Decoder self-attention
    expected = torch.tensor([block_tables[0] for _ in range(len(seq_group_metadata_list))],
                            dtype=torch.int32,
                            device=model_runner.device)
    assert torch.allclose(attn_metadata.block_tables, expected)
    # - Encoder/decoder cross-attention
    expected = torch.tensor([cross_block_table for _ in range(len(seq_group_metadata_list))],
                            dtype=torch.int32,
                            device=model_runner.device)
    assert torch.allclose(attn_metadata.cross_block_tables, expected)

    # Cuda graph should not be used for prefill.
    assert attn_metadata.use_cuda_graph == (not enforce_eager)

    # Verify the lengths of input tokens & positions
    # - Decoder
    assert len(input_tokens) == len(seq_lens)
    assert len(input_positions) == len(seq_lens)
    torch.testing.assert_close(input_tokens, 
                               input_positions)
    # - Encoder
    assert len(encoder_input_tokens) == 0
    assert len(encoder_input_positions) == 0

@pytest.mark.parametrize("backend_name", BACKEND_NAMES)
@pytest.mark.parametrize("enforce_eager", ENFORCE_EAGER)
def test_empty_seq_group(backend_name, enforce_eager, monkeypatch):
    """Verify prepare prompt and decode returns empty output."""

    # Force Attention wrapper backend
    override_backend_env_variable(monkeypatch, backend_name)

    model_runner = _create_model_runner(
        "facebook/bart-base",
        seed=0,
        dtype="float16",
        enforce_eager=enforce_eager,
    )
    seq_group_metadata_list: List[SequenceGroupMetadata] = []
    model_input = model_runner._prepare_model_input(seq_group_metadata_list)
    input_tokens, input_positions, attn_metadata, slot_mapping = (
        model_input.input_tokens,
        model_input.input_positions,
        model_input.attn_metadata,
        model_input.slot_mapping,
    )
    assert len(input_tokens) == 0
    assert len(input_positions) == 0
    assert attn_metadata is None
    assert len(slot_mapping) == 0

    model_input = model_runner._prepare_model_input(seq_group_metadata_list)
    (input_tokens, input_positions, attn_metadata, slot_mapping,
     return_seq_lens) = (
         model_input.input_tokens,
         model_input.input_positions,
         model_input.attn_metadata,
         model_input.slot_mapping,
         model_input.seq_lens,
     )
    assert len(input_tokens) == 0
    assert len(input_positions) == 0
    assert attn_metadata is None
    assert len(slot_mapping) == 0
    assert len(return_seq_lens) == 0
