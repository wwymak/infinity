# SPDX-License-Identifier: MIT
# Copyright (c) 2023-now michaelfeil

import copy
import os

import numpy as np

from infinity_emb._optional_imports import CHECK_ONNXRUNTIME, CHECK_TRANSFORMERS
from infinity_emb.args import EngineArgs
from infinity_emb.primitives import EmbeddingReturnType, PoolingMethod
from infinity_emb.transformer.abstract import BaseClassifer
from infinity_emb.transformer.quantization.interface import quant_embedding_decorator
from infinity_emb.transformer.utils_optimum import (
    cls_token_pooling,
    device_to_onnx,
    get_onnx_files,
    mean_pooling,
    normalize,
    optimize_model,
)

if CHECK_ONNXRUNTIME.is_available:
    try:
        from optimum.onnxruntime import (  # type: ignore[import-untyped]
            ORTModelForSequenceClassification,
        )

    except (ImportError, RuntimeError, Exception) as ex:
        CHECK_ONNXRUNTIME.mark_dirty(ex)

if CHECK_TRANSFORMERS.is_available:
    from transformers import AutoConfig, AutoTokenizer, pipeline  # type: ignore[import-untyped]


class OptimumClassifier(BaseClassifer):
    def __init__(self, *, engine_args: EngineArgs):
        CHECK_ONNXRUNTIME.mark_required()
        CHECK_TRANSFORMERS.mark_required()
        provider = device_to_onnx(engine_args.device)

        onnx_file = get_onnx_files(
            model_name_or_path=engine_args.model_name_or_path,
            revision=engine_args.revision,
            use_auth_token=True,
            prefer_quantized=("cpu" in provider.lower() or "openvino" in provider.lower()),
        )

        self.pooling = (
            mean_pooling if engine_args.pooling_method == PoolingMethod.mean else cls_token_pooling
        )

        self.model = optimize_model(
            model_name_or_path=engine_args.model_name_or_path,
            model_class=ORTModelForSequenceClassification,
            revision=engine_args.revision,
            trust_remote_code=engine_args.trust_remote_code,
            execution_provider=provider,
            file_name=onnx_file.as_posix(),
            optimize_model=not os.environ.get(
                "INFINITY_ONNX_DISABLE_OPTIMIZE", False
            ),
        )
        self.model.use_io_binding = False

        self.tokenizer = AutoTokenizer.from_pretrained(
            engine_args.model_name_or_path,
            revision=engine_args.revision,
            trust_remote_code=engine_args.trust_remote_code,
        )
        self.config = AutoConfig.from_pretrained(
            engine_args.model_name_or_path,
            revision=engine_args.revision,
            trust_remote_code=engine_args.trust_remote_code,
        )
        self._infinity_tokenizer = copy.deepcopy(self.tokenizer)
        self.engine_args = engine_args
        self._pipe = pipeline(
            task="text-classification",
            model=self.model,
            tokenizer=self.tokenizer,
            device=engine_args.device,
        )

    def encode_pre(self, sentences: list[str]):
        return sentences

    def encode_core(self, sentences: list[str]) -> dict:
        outputs = self._pipe(sentences)
        return outputs

    def encode_post(self, classes) -> dict[str, float]:
        """runs post encoding such as normalization"""
        return classes

    def tokenize_lengths(self, sentences: list[str]) -> list[int]:
        """gets the lengths of each sentences according to tokenize/len etc."""
        tks = self._infinity_tokenizer.batch_encode_plus(
            sentences,
            add_special_tokens=False,
            return_token_type_ids=False,
            return_attention_mask=False,
            return_length=False,
        ).encodings
        return [len(t.tokens) for t in tks]