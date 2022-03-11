#!/usr/bin/env python3
# Copyright (c)  2021  University of Chinese Academy of Sciences (author: Han Zhu)
#
# See ../../../../LICENSE for clarification regarding multiple authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import math
import warnings
from typing import Optional, Tuple, Sequence
from subsampling import PeLU, ExpScale, SwishExpScale, ExpScaleRelu, DerivBalancer, BasicNorm

import torch
from torch import Tensor, nn
from transformer import Transformer

from icefall.utils import make_pad_mask


class Conformer(Transformer):
    """
    Args:
        num_features (int): Number of input features
        output_dim (int): Number of output dimension
        subsampling_factor (int): subsampling factor of encoder (the convolution layers before transformers)
        d_model (int): attention dimension
        nhead (int): number of head
        dim_feedforward (int): feedforward dimention
        num_encoder_layers (int): number of encoder layers
        dropout (float): dropout rate
        cnn_module_kernel (int): Kernel size of convolution module
        normalize_before (bool): whether to use layer_norm before the first block.
        vgg_frontend (bool): whether to use vgg frontend.
    """

    def __init__(
        self,
        num_features: int,
        output_dim: int,
        subsampling_factor: int = 4,
        d_model: int = 256,
        nhead: int = 4,
        dim_feedforward: int = 2048,
        num_encoder_layers: int = 12,
        dropout: float = 0.1,
        cnn_module_kernel: int = 31,
        normalize_before: bool = True,
        vgg_frontend: bool = False,
        aux_layer_period: int = 3
    ) -> None:
        super(Conformer, self).__init__(
            num_features=num_features,
            output_dim=output_dim,
            subsampling_factor=subsampling_factor,
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=dim_feedforward,
            num_encoder_layers=num_encoder_layers,
            dropout=dropout,
            normalize_before=normalize_before,
            vgg_frontend=vgg_frontend,
        )

        self.encoder_pos = RelPositionalEncoding(d_model, dropout)

        encoder_layer = ConformerEncoderLayer(
            d_model,
            nhead,
            dim_feedforward,
            dropout,
            cnn_module_kernel,
            normalize_before,
        )
        self.encoder = ConformerEncoder(encoder_layer, num_encoder_layers,
                                        aux_layers=list(range(0, num_encoder_layers-1, aux_layer_period)))
        self.normalize_before = normalize_before


    def forward(
        self, x: torch.Tensor, x_lens: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Args:
          x:
            The input tensor. Its shape is (batch_size, seq_len, feature_dim).
          x_lens:
            A tensor of shape (batch_size,) containing the number of frames in
            `x` before padding.
        Returns:
          Return a tuple containing 2 tensors:
            - logits, its shape is (batch_size, output_seq_len, output_dim)
            - logit_lens, a tensor of shape (batch_size,) containing the number
              of frames in `logits` before padding.
        """
        x = self.encoder_embed(x)
        x, pos_emb = self.encoder_pos(x)
        x = x.permute(1, 0, 2)  # (N, T, C) -> (T, N, C)

        # Caution: We assume the subsampling factor is 4!
        lengths = ((x_lens - 1) // 2 - 1) // 2
        assert x.size(0) == lengths.max().item()
        mask = make_pad_mask(lengths)

        x = self.encoder(x, pos_emb, src_key_padding_mask=mask)  # (T, N, C)

        logits = self.encoder_output_layer(x)
        logits = logits.permute(1, 0, 2)  # (T, N, C) ->(N, T, C)

        return logits, lengths


class ConformerEncoderLayer(nn.Module):
    """
    ConformerEncoderLayer is made up of self-attn, feedforward and convolution networks.
    See: "Conformer: Convolution-augmented Transformer for Speech Recognition"

    Args:
        d_model: the number of expected features in the input (required).
        nhead: the number of heads in the multiheadattention models (required).
        dim_feedforward: the dimension of the feedforward network model (default=2048).
        dropout: the dropout value (default=0.1).
        cnn_module_kernel (int): Kernel size of convolution module.
        normalize_before: whether to use layer_norm before the first block.

    Examples::
        >>> encoder_layer = ConformerEncoderLayer(d_model=512, nhead=8)
        >>> src = torch.rand(10, 32, 512)
        >>> pos_emb = torch.rand(32, 19, 512)
        >>> out = encoder_layer(src, pos_emb)
    """

    def __init__(
        self,
        d_model: int,
        nhead: int,
        dim_feedforward: int = 2048,
        dropout: float = 0.1,
        cnn_module_kernel: int = 31,
        normalize_before: bool = True,
    ) -> None:
        super(ConformerEncoderLayer, self).__init__()
        self.d_model = d_model

        self.self_attn = RelPositionMultiheadAttention(
            d_model, nhead, dropout=0.0
        )

        self.feed_forward = nn.Sequential(
            nn.Linear(d_model, dim_feedforward),
            DerivBalancer(channel_dim=-1, threshold=0.05,
                          max_factor=0.01),
            SwishExpScale(dim_feedforward, speed=20.0),
            nn.Dropout(dropout),
            nn.Linear(dim_feedforward, d_model),
        )

        self.feed_forward_macaron = nn.Sequential(
            nn.Linear(d_model, dim_feedforward),
            DerivBalancer(channel_dim=-1, threshold=0.05,
                          max_factor=0.01),
            SwishExpScale(dim_feedforward, speed=20.0),
            nn.Dropout(dropout),
            nn.Linear(dim_feedforward, d_model),
        )

        self.conv_module = ConvolutionModule(d_model, cnn_module_kernel)

        self.scale_mha = ExpScale(1, speed=10.0, initial_scale=0.2)
        self.scale_conv = ExpScale(1, speed=10.0, initial_scale=0.5)
        self.scale_ff = ExpScale(1, speed=10.0, initial_scale=0.5)
        self.scale_ff_macaron = ExpScale(1, speed=10.0, initial_scale=0.5)

        self.norm_final = BasicNorm(d_model)

        self.dropout = nn.Dropout(dropout)


    def forward(
        self,
        src: Tensor,
        pos_emb: Tensor,
        src_mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        """
        Pass the input through the encoder layer.

        Args:
            src: the sequence to the encoder layer (required).
            pos_emb: Positional embedding tensor (required).
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            src: (S, N, E).
            pos_emb: (N, 2*S-1, E)
            src_mask: (S, S).
            src_key_padding_mask: (N, S).
            S is the source sequence length, N is the batch size, E is the feature number
        """

        # macaron style feed forward module
        residual = src


        src = src + self.dropout(self.feed_forward_macaron(
            self.scale_ff_macaron(src)))


        # multi-headed self-attention module
        residual = src
        src = self.scale_mha(src)
        src_att = self.self_attn(
            src,
            src,
            src,
            pos_emb=pos_emb,
            attn_mask=src_mask,
            key_padding_mask=src_key_padding_mask,
        )[0]
        src = residual + self.dropout(src_att)

        # convolution module
        src = residual + self.dropout(self.conv_module(self.scale_conv(src)))

        # feed forward module
        src = src +  self.dropout(self.feed_forward(self.scale_ff(src)))

        src = self.norm_final(src)

        return src


class ConformerEncoder(nn.Module):
    r"""ConformerEncoder is a stack of N encoder layers

    Args:
        encoder_layer: an instance of the ConformerEncoderLayer() class (required).
        num_layers: the number of sub-encoder-layers in the encoder (required).

    Examples::
        >>> encoder_layer = ConformerEncoderLayer(d_model=512, nhead=8)
        >>> conformer_encoder = ConformerEncoder(encoder_layer, num_layers=6)
        >>> src = torch.rand(10, 32, 512)
        >>> pos_emb = torch.rand(32, 19, 512)
        >>> out = conformer_encoder(src, pos_emb)
    """

    def __init__(
            self, encoder_layer: nn.Module,
            num_layers: int,
            aux_layers: Sequence[int],
    ) -> None:
        super(ConformerEncoder, self).__init__()
        self.layers = nn.ModuleList([copy.deepcopy(encoder_layer) for i in range(num_layers)])
        self.aux_layers = set(aux_layers + [num_layers - 1])
        assert num_layers - 1 not in aux_layers
        self.num_layers = num_layers
        num_channels = encoder_layer.d_model
        self.combiner = RandomCombine(num_inputs=len(self.aux_layers),
                                      num_channels=num_channels,
                                      final_weight=0.5,
                                      pure_prob=0.333,
                                      stddev=2.0)

    def forward(
        self,
        src: Tensor,
        pos_emb: Tensor,
        mask: Optional[Tensor] = None,
        src_key_padding_mask: Optional[Tensor] = None,
    ) -> Tensor:
        r"""Pass the input through the encoder layers in turn.

        Args:
            src: the sequence to the encoder (required).
            pos_emb: Positional embedding tensor (required).
            mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            src: (S, N, E).
            pos_emb: (N, 2*S-1, E)
            mask: (S, S).
            src_key_padding_mask: (N, S).
            S is the source sequence length, T is the target sequence length, N is the batch size, E is the feature number

        """
        output = src

        outputs = []

        for i, mod in enumerate(self.layers):
            output = mod(
                output,
                pos_emb,
                src_mask=mask,
                src_key_padding_mask=src_key_padding_mask,
            )
            if i in self.aux_layers:
                outputs.append(output)

        output = self.combiner(outputs)
        return output


class RelPositionalEncoding(torch.nn.Module):
    """Relative positional encoding module.

    See : Appendix B in "Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context"
    Modified from https://github.com/espnet/espnet/blob/master/espnet/nets/pytorch_backend/transformer/embedding.py

    Args:
        d_model: Embedding dimension.
        dropout_rate: Dropout rate.
        max_len: Maximum input length.

    """

    def __init__(
        self, d_model: int, dropout_rate: float, max_len: int = 5000
    ) -> None:
        """Construct an PositionalEncoding object."""
        super(RelPositionalEncoding, self).__init__()
        self.d_model = d_model
        self.xscale = math.sqrt(self.d_model)
        self.dropout = torch.nn.Dropout(p=dropout_rate)
        self.pe = None
        self.extend_pe(torch.tensor(0.0).expand(1, max_len))

    def extend_pe(self, x: Tensor) -> None:
        """Reset the positional encodings."""
        if self.pe is not None:
            # self.pe contains both positive and negative parts
            # the length of self.pe is 2 * input_len - 1
            if self.pe.size(1) >= x.size(1) * 2 - 1:
                # Note: TorchScript doesn't implement operator== for torch.Device
                if self.pe.dtype != x.dtype or str(self.pe.device) != str(
                    x.device
                ):
                    self.pe = self.pe.to(dtype=x.dtype, device=x.device)
                return
        # Suppose `i` means to the position of query vecotr and `j` means the
        # position of key vector. We use position relative positions when keys
        # are to the left (i>j) and negative relative positions otherwise (i<j).
        pe_positive = torch.zeros(x.size(1), self.d_model)
        pe_negative = torch.zeros(x.size(1), self.d_model)
        position = torch.arange(0, x.size(1), dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, self.d_model, 2, dtype=torch.float32)
            * -(math.log(10000.0) / self.d_model)
        )
        pe_positive[:, 0::2] = torch.sin(position * div_term)
        pe_positive[:, 1::2] = torch.cos(position * div_term)
        pe_negative[:, 0::2] = torch.sin(-1 * position * div_term)
        pe_negative[:, 1::2] = torch.cos(-1 * position * div_term)

        # Reserve the order of positive indices and concat both positive and
        # negative indices. This is used to support the shifting trick
        # as in "Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context"
        pe_positive = torch.flip(pe_positive, [0]).unsqueeze(0)
        pe_negative = pe_negative[1:].unsqueeze(0)
        pe = torch.cat([pe_positive, pe_negative], dim=1)
        self.pe = pe.to(device=x.device, dtype=x.dtype)

    def forward(self, x: torch.Tensor) -> Tuple[Tensor, Tensor]:
        """Add positional encoding.

        Args:
            x (torch.Tensor): Input tensor (batch, time, `*`).

        Returns:
            torch.Tensor: Encoded tensor (batch, time, `*`).
            torch.Tensor: Encoded tensor (batch, 2*time-1, `*`).

        """
        self.extend_pe(x)
        x = x * self.xscale
        pos_emb = self.pe[
            :,
            self.pe.size(1) // 2
            - x.size(1)
            + 1 : self.pe.size(1) // 2  # noqa E203
            + x.size(1),
        ]
        return self.dropout(x), self.dropout(pos_emb)


class RelPositionMultiheadAttention(nn.Module):
    r"""Multi-Head Attention layer with relative position encoding

    See reference: "Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context"

    Args:
        embed_dim: total dimension of the model.
        num_heads: parallel attention heads.
        dropout: a Dropout layer on attn_output_weights. Default: 0.0.

    Examples::

        >>> rel_pos_multihead_attn = RelPositionMultiheadAttention(embed_dim, num_heads)
        >>> attn_output, attn_output_weights = multihead_attn(query, key, value, pos_emb)
    """

    def __init__(
        self,
        embed_dim: int,
        num_heads: int,
        dropout: float = 0.0,
    ) -> None:
        super(RelPositionMultiheadAttention, self).__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert (
            self.head_dim * num_heads == self.embed_dim
        ), "embed_dim must be divisible by num_heads"

        self.in_proj = nn.Linear(embed_dim, 3 * embed_dim, bias=True)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=True)

        # linear transformation for positional encoding.
        self.linear_pos = nn.Linear(embed_dim, embed_dim, bias=False)
        # these two learnable bias are used in matrix c and matrix d
        # as described in "Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context" Section 3.3
        self.pos_bias_u = nn.Parameter(torch.Tensor(num_heads, self.head_dim))
        self.pos_bias_v = nn.Parameter(torch.Tensor(num_heads, self.head_dim))

        self._reset_parameters()

    def _reset_parameters(self) -> None:
        nn.init.xavier_uniform_(self.in_proj.weight)
        nn.init.constant_(self.in_proj.bias, 0.0)
        nn.init.constant_(self.out_proj.bias, 0.0)

        nn.init.xavier_uniform_(self.pos_bias_u)
        nn.init.xavier_uniform_(self.pos_bias_v)

    def forward(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        pos_emb: Tensor,
        key_padding_mask: Optional[Tensor] = None,
        need_weights: bool = True,
        attn_mask: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        r"""
        Args:
            query, key, value: map a query and a set of key-value pairs to an output.
            pos_emb: Positional embedding tensor
            key_padding_mask: if provided, specified padding elements in the key will
                be ignored by the attention. When given a binary mask and a value is True,
                the corresponding value on the attention layer will be ignored. When given
                a byte mask and a value is non-zero, the corresponding value on the attention
                layer will be ignored
            need_weights: output attn_output_weights.
            attn_mask: 2D or 3D mask that prevents attention to certain positions. A 2D mask will be broadcasted for all
                the batches while a 3D mask allows to specify a different mask for the entries of each batch.

        Shape:
            - Inputs:
            - query: :math:`(L, N, E)` where L is the target sequence length, N is the batch size, E is
            the embedding dimension.
            - key: :math:`(S, N, E)`, where S is the source sequence length, N is the batch size, E is
            the embedding dimension.
            - value: :math:`(S, N, E)` where S is the source sequence length, N is the batch size, E is
            the embedding dimension.
            - pos_emb: :math:`(N, 2*L-1, E)` where L is the target sequence length, N is the batch size, E is
            the embedding dimension.
            - key_padding_mask: :math:`(N, S)` where N is the batch size, S is the source sequence length.
            If a ByteTensor is provided, the non-zero positions will be ignored while the position
            with the zero positions will be unchanged. If a BoolTensor is provided, the positions with the
            value of ``True`` will be ignored while the position with the value of ``False`` will be unchanged.
            - attn_mask: 2D mask :math:`(L, S)` where L is the target sequence length, S is the source sequence length.
            3D mask :math:`(N*num_heads, L, S)` where N is the batch size, L is the target sequence length,
            S is the source sequence length. attn_mask ensure that position i is allowed to attend the unmasked
            positions. If a ByteTensor is provided, the non-zero positions are not allowed to attend
            while the zero positions will be unchanged. If a BoolTensor is provided, positions with ``True``
            is not allowed to attend while ``False`` values will be unchanged. If a FloatTensor
            is provided, it will be added to the attention weight.

            - Outputs:
            - attn_output: :math:`(L, N, E)` where L is the target sequence length, N is the batch size,
            E is the embedding dimension.
            - attn_output_weights: :math:`(N, L, S)` where N is the batch size,
            L is the target sequence length, S is the source sequence length.
        """
        return self.multi_head_attention_forward(
            query,
            key,
            value,
            pos_emb,
            self.embed_dim,
            self.num_heads,
            self.in_proj.weight,
            self.in_proj.bias,
            self.dropout,
            self.out_proj.weight,
            self.out_proj.bias,
            training=self.training,
            key_padding_mask=key_padding_mask,
            need_weights=need_weights,
            attn_mask=attn_mask,
        )

    def rel_shift(self, x: Tensor) -> Tensor:
        """Compute relative positional encoding.

        Args:
            x: Input tensor (batch, head, time1, 2*time1-1).
                time1 means the length of query vector.

        Returns:
            Tensor: tensor of shape (batch, head, time1, time2)
          (note: time2 has the same value as time1, but it is for
          the key, while time1 is for the query).
        """
        (batch_size, num_heads, time1, n) = x.shape
        assert n == 2 * time1 - 1
        # Note: TorchScript requires explicit arg for stride()
        batch_stride = x.stride(0)
        head_stride = x.stride(1)
        time1_stride = x.stride(2)
        n_stride = x.stride(3)
        return x.as_strided(
            (batch_size, num_heads, time1, time1),
            (batch_stride, head_stride, time1_stride - n_stride, n_stride),
            storage_offset=n_stride * (time1 - 1),
        )

    def multi_head_attention_forward(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        pos_emb: Tensor,
        embed_dim_to_check: int,
        num_heads: int,
        in_proj_weight: Tensor,
        in_proj_bias: Tensor,
        dropout_p: float,
        out_proj_weight: Tensor,
        out_proj_bias: Tensor,
        training: bool = True,
        key_padding_mask: Optional[Tensor] = None,
        need_weights: bool = True,
        attn_mask: Optional[Tensor] = None,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        r"""
        Args:
            query, key, value: map a query and a set of key-value pairs to an output.
            pos_emb: Positional embedding tensor
            embed_dim_to_check: total dimension of the model.
            num_heads: parallel attention heads.
            in_proj_weight, in_proj_bias: input projection weight and bias.
            dropout_p: probability of an element to be zeroed.
            out_proj_weight, out_proj_bias: the output projection weight and bias.
            training: apply dropout if is ``True``.
            key_padding_mask: if provided, specified padding elements in the key will
                be ignored by the attention. This is an binary mask. When the value is True,
                the corresponding value on the attention layer will be filled with -inf.
            need_weights: output attn_output_weights.
            attn_mask: 2D or 3D mask that prevents attention to certain positions. A 2D mask will be broadcasted for all
                the batches while a 3D mask allows to specify a different mask for the entries of each batch.

        Shape:
            Inputs:
            - query: :math:`(L, N, E)` where L is the target sequence length, N is the batch size, E is
            the embedding dimension.
            - key: :math:`(S, N, E)`, where S is the source sequence length, N is the batch size, E is
            the embedding dimension.
            - value: :math:`(S, N, E)` where S is the source sequence length, N is the batch size, E is
            the embedding dimension.
            - pos_emb: :math:`(N, 2*L-1, E)` or :math:`(1, 2*L-1, E)` where L is the target sequence
            length, N is the batch size, E is the embedding dimension.
            - key_padding_mask: :math:`(N, S)` where N is the batch size, S is the source sequence length.
            If a ByteTensor is provided, the non-zero positions will be ignored while the zero positions
            will be unchanged. If a BoolTensor is provided, the positions with the
            value of ``True`` will be ignored while the position with the value of ``False`` will be unchanged.
            - attn_mask: 2D mask :math:`(L, S)` where L is the target sequence length, S is the source sequence length.
            3D mask :math:`(N*num_heads, L, S)` where N is the batch size, L is the target sequence length,
            S is the source sequence length. attn_mask ensures that position i is allowed to attend the unmasked
            positions. If a ByteTensor is provided, the non-zero positions are not allowed to attend
            while the zero positions will be unchanged. If a BoolTensor is provided, positions with ``True``
            are not allowed to attend while ``False`` values will be unchanged. If a FloatTensor
            is provided, it will be added to the attention weight.

            Outputs:
            - attn_output: :math:`(L, N, E)` where L is the target sequence length, N is the batch size,
            E is the embedding dimension.
            - attn_output_weights: :math:`(N, L, S)` where N is the batch size,
            L is the target sequence length, S is the source sequence length.
        """

        tgt_len, bsz, embed_dim = query.size()
        assert embed_dim == embed_dim_to_check
        assert key.size(0) == value.size(0) and key.size(1) == value.size(1)

        head_dim = embed_dim // num_heads
        assert (
            head_dim * num_heads == embed_dim
        ), "embed_dim must be divisible by num_heads"

        scaling = float(head_dim) ** -0.5

        if torch.equal(query, key) and torch.equal(key, value):
            # self-attention
            q, k, v = nn.functional.linear(query, in_proj_weight, in_proj_bias).chunk(3, dim=-1)

        elif torch.equal(key, value):
            # encoder-decoder attention
            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = 0
            _end = embed_dim
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            q = nn.functional.linear(query, _w, _b)

            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = embed_dim
            _end = None
            _w = in_proj_weight[_start:, :]
            if _b is not None:
                _b = _b[_start:]
            k, v = nn.functional.linear(key, _w, _b).chunk(2, dim=-1)

        else:
            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = 0
            _end = embed_dim
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            q = nn.functional.linear(query, _w, _b)


            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = embed_dim
            _end = embed_dim * 2
            _w = in_proj_weight[_start:_end, :]
            if _b is not None:
                _b = _b[_start:_end]
            k = nn.functional.linear(key, _w, _b)

            # This is inline in_proj function with in_proj_weight and in_proj_bias
            _b = in_proj_bias
            _start = embed_dim * 2
            _end = None
            _w = in_proj_weight[_start:, :]
            if _b is not None:
                _b = _b[_start:]
            v = nn.functional.linear(value, _w, _b)


        if attn_mask is not None:
            assert (
                attn_mask.dtype == torch.float32
                or attn_mask.dtype == torch.float64
                or attn_mask.dtype == torch.float16
                or attn_mask.dtype == torch.uint8
                or attn_mask.dtype == torch.bool
            ), "Only float, byte, and bool types are supported for attn_mask, not {}".format(
                attn_mask.dtype
            )
            if attn_mask.dtype == torch.uint8:
                warnings.warn(
                    "Byte tensor for attn_mask is deprecated. Use bool tensor instead."
                )
                attn_mask = attn_mask.to(torch.bool)

            if attn_mask.dim() == 2:
                attn_mask = attn_mask.unsqueeze(0)
                if list(attn_mask.size()) != [1, query.size(0), key.size(0)]:
                    raise RuntimeError(
                        "The size of the 2D attn_mask is not correct."
                    )
            elif attn_mask.dim() == 3:
                if list(attn_mask.size()) != [
                    bsz * num_heads,
                    query.size(0),
                    key.size(0),
                ]:
                    raise RuntimeError(
                        "The size of the 3D attn_mask is not correct."
                    )
            else:
                raise RuntimeError(
                    "attn_mask's dimension {} is not supported".format(
                        attn_mask.dim()
                    )
                )
            # attn_mask's dim is 3 now.

        # convert ByteTensor key_padding_mask to bool
        if (
            key_padding_mask is not None
            and key_padding_mask.dtype == torch.uint8
        ):
            warnings.warn(
                "Byte tensor for key_padding_mask is deprecated. Use bool tensor instead."
            )
            key_padding_mask = key_padding_mask.to(torch.bool)

        q = (q * scaling).contiguous().view(tgt_len, bsz, num_heads, head_dim)
        k = k.contiguous().view(-1, bsz, num_heads, head_dim)
        v = v.contiguous().view(-1, bsz * num_heads, head_dim).transpose(0, 1)

        src_len = k.size(0)

        if key_padding_mask is not None:
            assert key_padding_mask.size(0) == bsz, "{} == {}".format(
                key_padding_mask.size(0), bsz
            )
            assert key_padding_mask.size(1) == src_len, "{} == {}".format(
                key_padding_mask.size(1), src_len
            )

        q = q.transpose(0, 1)  # (batch, time1, head, d_k)

        pos_emb_bsz = pos_emb.size(0)
        assert pos_emb_bsz in (1, bsz)  # actually it is 1
        p = self.linear_pos(pos_emb).view(pos_emb_bsz, -1, num_heads, head_dim)
        p = p.transpose(1, 2)  # (batch, head, 2*time1-1, d_k)

        q_with_bias_u = (q + self.pos_bias_u).transpose(
            1, 2
        )  # (batch, head, time1, d_k)

        q_with_bias_v = (q + self.pos_bias_v).transpose(
            1, 2
        )  # (batch, head, time1, d_k)

        # compute attention score
        # first compute matrix a and matrix c
        # as described in "Transformer-XL: Attentive Language Models Beyond a Fixed-Length Context" Section 3.3
        k = k.permute(1, 2, 3, 0)  # (batch, head, d_k, time2)
        matrix_ac = torch.matmul(
            q_with_bias_u, k
        )  # (batch, head, time1, time2)

        # compute matrix b and matrix d
        matrix_bd = torch.matmul(
            q_with_bias_v, p.transpose(-2, -1)
        )  # (batch, head, time1, 2*time1-1)
        matrix_bd = self.rel_shift(matrix_bd)

        attn_output_weights = (
            matrix_ac + matrix_bd
        )  # (batch, head, time1, time2)

        attn_output_weights = attn_output_weights.view(
            bsz * num_heads, tgt_len, -1
        )

        assert list(attn_output_weights.size()) == [
            bsz * num_heads,
            tgt_len,
            src_len,
        ]

        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
                attn_output_weights.masked_fill_(attn_mask, float("-inf"))
            else:
                attn_output_weights += attn_mask

        if key_padding_mask is not None:
            attn_output_weights = attn_output_weights.view(
                bsz, num_heads, tgt_len, src_len
            )
            attn_output_weights = attn_output_weights.masked_fill(
                key_padding_mask.unsqueeze(1).unsqueeze(2),
                float("-inf"),
            )
            attn_output_weights = attn_output_weights.view(
                bsz * num_heads, tgt_len, src_len
            )

        attn_output_weights = nn.functional.softmax(attn_output_weights, dim=-1)
        attn_output_weights = nn.functional.dropout(
            attn_output_weights, p=dropout_p, training=training
        )

        attn_output = torch.bmm(attn_output_weights, v)
        assert list(attn_output.size()) == [bsz * num_heads, tgt_len, head_dim]
        attn_output = (
            attn_output.transpose(0, 1)
            .contiguous()
            .view(tgt_len, bsz, embed_dim)
        )
        attn_output = nn.functional.linear(
            attn_output, out_proj_weight, out_proj_bias
        )

        if need_weights:
            # average attention weights over heads
            attn_output_weights = attn_output_weights.view(
                bsz, num_heads, tgt_len, src_len
            )
            return attn_output, attn_output_weights.sum(dim=1) / num_heads
        else:
            return attn_output, None


class ConvolutionModule(nn.Module):
    """ConvolutionModule in Conformer model.
    Modified from https://github.com/espnet/espnet/blob/master/espnet/nets/pytorch_backend/conformer/convolution.py

    Args:
        channels (int): The number of channels of conv layers.
        kernel_size (int): Kernerl size of conv layers.
        bias (bool): Whether to use bias in conv layers (default=True).

    """

    def __init__(
        self, channels: int, kernel_size: int, bias: bool = True
    ) -> None:
        """Construct an ConvolutionModule object."""
        super(ConvolutionModule, self).__init__()
        # kernerl_size should be a odd number for 'SAME' padding
        assert (kernel_size - 1) % 2 == 0

        self.pointwise_conv1 = nn.Conv1d(
            channels,
            2 * channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=bias,
        )
        self.depthwise_conv = nn.Conv1d(
            channels,
            channels,
            kernel_size,
            stride=1,
            padding=(kernel_size - 1) // 2,
            groups=channels,
            bias=bias,
        )

        self.scale = ExpScale(1, speed=10.0, initial_scale=1.0)

         # shape: (channels, 1), broadcasts with (batch, channel, time).
        self.activation = SwishOffset()

        self.pointwise_conv2 = nn.Conv1d(
            channels,
            channels,
            kernel_size=1,
            stride=1,
            padding=0,
            bias=bias,
        )

    def forward(self, x: Tensor) -> Tensor:
        """Compute convolution module.

        Args:
            x: Input tensor (#time, batch, channels).

        Returns:
            Tensor: Output tensor (#time, batch, channels).

        """
        # exchange the temporal dimension and the feature dimension
        x = x.permute(1, 2, 0)  # (#batch, channels, time).

        # GLU mechanism
        x = self.pointwise_conv1(x)  # (batch, 2*channels, time)
        x = nn.functional.glu(x, dim=1)  # (batch, channels, time)

        # 1D Depthwise Conv
        x = self.depthwise_conv(x)
        # x is (batch, channels, time)
        x = x.permute(0, 2, 1)
        x = self.scale(x)
        x = x.permute(0, 2, 1)

        x = self.activation(x)

        x = self.pointwise_conv2(x)  # (batch, channel, time)

        return x.permute(2, 0, 1)


class Swish(torch.nn.Module):
    """Construct an Swish object."""

    def forward(self, x: Tensor) -> Tensor:
        """Return Swich activation function."""
        return x * torch.sigmoid(x)

class SwishOffset(torch.nn.Module):
    """Construct an SwishOffset object."""
    def __init__(self, offset: float = -1.0) -> None:
        super(SwishOffset, self).__init__()
        self.offset = offset

    def forward(self, x: Tensor) -> Tensor:
        """Return Swich activation function."""
        return x * torch.sigmoid(x + self.offset)


def identity(x):
    return x


class RandomCombine(torch.nn.Module):
    """
    This module combines a list of Tensors, all with the same shape, to
    produce a single output of that same shape which, in training time,
    is a random combination of all the inputs; but which in test time
    will be just the last input.

    All but the last input will have a linear transform before we
    randomly combine them; these linear transforms will be initialzed
    to the identity transform.

    The idea is that the list of Tensors will be a list of outputs of multiple
    conformer layers.  This has a similar effect as iterated loss. (See:
    DEJA-VU: DOUBLE FEATURE PRESENTATION AND ITERATED LOSS IN DEEP TRANSFORMER
    NETWORKS).
    """
    def __init__(self, num_inputs: int,
                 num_channels: int,
                 final_weight: float = 0.5,
                 pure_prob: float = 0.5,
                 stddev: float = 2.0) -> None:
        """
        Args:
          num_inputs:  The number of tensor inputs, which equals the number of layers'
                outputs that are fed into this module.  E.g. in an 18-layer neural
                net if we output layers 16, 12, 18, num_inputs would be 3.
          num_channels:  The number of channels on the input, e.g. 512.
          final_weight:  The amount of weight or probability we assign to the
                final layer when randomly choosing layers or when choosing
                continuous layer weights.
          pure_prob: The probability, on each frame, with which we choose
                only a single layer to output (rather than an interpolation)
          stddev:  A standard deviation that we add to log-probs for computing
                randomized weights.

         The method of choosing which layers,
                or combinations of layers, to use, is conceptually as follows.
                    With probability `pure_prob`:
                       With probability `final_weight`: choose final layer,
                       Else: choose random non-final layer.
                    Else:
                       Choose initial log-weights that correspond to assigning
                       weight `final_weight` to the final layer and equal
                       weights to other layers; then add Gaussian noise
                       with variance `stddev` to these log-weights, and normalize
                       to weights (note: the average weight assigned to the
                       final layer here will not be `final_weight` if stddev>0).
        """
        super(RandomCombine, self).__init__()
        assert pure_prob >= 0 and pure_prob <= 1
        assert final_weight > 0 and final_weight < 1
        assert num_inputs >= 1
        self.linear = nn.ModuleList([nn.Linear(num_channels, num_channels, bias=True)
                                     for _ in range(num_inputs - 1)])

        self.num_inputs = num_inputs
        self.final_weight = final_weight
        self.pure_prob = pure_prob
        self.stddev= stddev

        self.final_log_weight = torch.tensor((final_weight / (1 - final_weight)) * (self.num_inputs - 1)).log().item()
        self._reset_parameters()

    def _reset_parameters(self):
        for i in range(len(self.linear)):
            nn.init.eye_(self.linear[i].weight)
            nn.init.constant_(self.linear[i].bias, 0.0)

    def forward(self, inputs: Sequence[Tensor]) -> Tensor:
        """
        Forward function.
       Args:
          inputs: a list of Tensor, e.g. from various layers of a transformer.
               All must be the same shape, of (*, num_channels)
       Returns:
          a Tensor of shape (*, num_channels).  In test mode
          this is just the final input.
        """
        num_inputs = self.num_inputs
        assert len(inputs) == num_inputs
        if not self.training:
            return inputs[-1]

        # Shape of weights: (*, num_inputs)
        num_channels = inputs[0].shape[-1]
        num_frames = inputs[0].numel() // num_channels

        mod_inputs = []
        for i in range(num_inputs - 1):
            mod_inputs.append(self.linear[i](inputs[i]))
        mod_inputs.append(inputs[num_inputs - 1])


        ndim = inputs[0].ndim
        # stacked_inputs: (num_frames, num_channels, num_inputs)
        stacked_inputs = torch.stack(mod_inputs, dim=ndim).reshape((num_frames,
                                                                    num_channels,
                                                                    num_inputs))

        # weights: (num_frames, num_inputs)
        weights = self._get_random_weights(inputs[0].dtype, inputs[0].device,
                                           num_frames)

        weights = weights.reshape(num_frames, num_inputs, 1)
        # ans: (num_frames, num_channels, 1)
        ans = torch.matmul(stacked_inputs, weights)
        # ans: (*, num_channels)
        ans = ans.reshape(*tuple(inputs[0].shape[:-1]), num_channels)

        if __name__ == "__main__":
            # for testing only...
            print("Weights = ", weights.reshape(num_frames, num_inputs))
        return ans


    def _get_random_weights(self, dtype: torch.dtype, device: torch.device, num_frames: int) -> Tensor:
        """
        Return a tensor of random weights, of shape (num_frames, self.num_inputs),
        Args:
            dtype: the data-type desired for the answer, e.g. float, double
           device: the device needed for the answer
          num_frames: the number of sets of weights desired
        Returns:  a tensor of shape (num_frames, self.num_inputs), such that
            ans.sum(dim=1) is all ones.

        """
        pure_prob = self.pure_prob
        if pure_prob == 0.0:
            return self._get_random_mixed_weights(dtype, device, num_frames)
        elif pure_prob == 1.0:
            return self._get_random_pure_weights(dtype, device, num_frames)
        else:
            p = self._get_random_pure_weights(dtype, device, num_frames)
            m = self._get_random_mixed_weights(dtype, device, num_frames)
            return torch.where(torch.rand(num_frames, 1, device=device) < self.pure_prob, p, m)

    def _get_random_pure_weights(self, dtype: torch.dtype, device: torch.device, num_frames: int):
        """
        Return a tensor of random one-hot weights, of shape (num_frames, self.num_inputs),
        Args:
            dtype: the data-type desired for the answer, e.g. float, double
           device: the device needed for the answer
          num_frames: the number of sets of weights desired
        Returns:  a one-hot tensor of shape (num_frames, self.num_inputs), with
          exactly one weight equal to 1.0 on each frame.
        """

        final_prob = self.final_weight

        # final contains self.num_inputs - 1 in all elements
        final = torch.full((num_frames,), self.num_inputs - 1, device=device)
        # nonfinal contains random integers in [0..num_inputs - 2], these are for non-final weights.
        nonfinal = torch.randint(self.num_inputs - 1, (num_frames,), device=device)

        indexes = torch.where(torch.rand(num_frames, device=device) < final_prob,
                              final, nonfinal)
        ans = torch.nn.functional.one_hot(indexes, num_classes=self.num_inputs).to(dtype=dtype)
        return ans


    def _get_random_mixed_weights(self, dtype: torch.dtype, device: torch.device, num_frames: int):
        """
        Return a tensor of random one-hot weights, of shape (num_frames, self.num_inputs),
        Args:
            dtype: the data-type desired for the answer, e.g. float, double
           device: the device needed for the answer
          num_frames: the number of sets of weights desired
        Returns:  a tensor of shape (num_frames, self.num_inputs), which elements in [0..1] that
          sum to one over the second axis, i.e. ans.sum(dim=1) is all ones.
        """
        logprobs = torch.randn(num_frames, self.num_inputs, dtype=dtype, device=device) * self.stddev
        logprobs[:,-1] += self.final_log_weight
        return logprobs.softmax(dim=1)


def _test_random_combine(final_weight: float, pure_prob: float, stddev: float):
    print(f"_test_random_combine: final_weight={final_weight}, pure_prob={pure_prob}, stddev={stddev}")
    num_inputs = 3
    num_channels = 50
    m = RandomCombine(num_inputs=num_inputs, num_channels=num_channels,
                      final_weight=final_weight, pure_prob=pure_prob, stddev=stddev)

    x = [ torch.ones(3, 4, num_channels) for _ in range(num_inputs) ]

    y = m(x)
    assert y.shape == x[0].shape
    assert torch.allclose(y, x[0]) # .. since actually all ones.


if __name__ == '__main__':
    _test_random_combine(0.999, 0, 0.0)
    _test_random_combine(0.5, 0, 0.0)
    _test_random_combine(0.999, 0, 0.0)
    _test_random_combine(0.5, 0, 0.3)
    _test_random_combine(0.5, 1, 0.3)
    _test_random_combine(0.5, 0.5, 0.3)

    feature_dim = 50
    c = Conformer(num_features=feature_dim, output_dim=256, d_model=128, nhead=4)
    batch_size = 5
    seq_len = 20
    # Just make sure the forward pass runs.
    f = c(torch.randn(batch_size, seq_len, feature_dim),
          torch.full((batch_size,), seq_len, dtype=torch.int64))
