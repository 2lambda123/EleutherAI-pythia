# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0
"""Streaming dataset conversion scripts for json files."""
import os
from argparse import ArgumentParser
from argparse import Namespace
from enum import Enum
from glob import glob
from typing import Dict
from typing import Iterable
from typing import Optional

import datasets as hf_datasets
import numpy as np
from llmfoundry.data import ConcatTokensDataset
from llmfoundry.data import NoConcatDataset
from streaming import MDSWriter
from torch.utils.data import DataLoader
from torch.utils.data import IterableDataset
from tqdm import tqdm
from transformers import AutoTokenizer
from transformers import PreTrainedTokenizerBase


class ConcatMode(Enum):
    """ """
    NO_CONCAT = "NO_CONCAT"
    CONCAT_TOKENS = "CONCAT_TOKENS"


def parse_args() -> Namespace:
    """Parse commandline arguments."""
    parser = ArgumentParser(
        description="Convert dataset into MDS format, optionally concatenating and tokenizing"
    )
    parser.add_argument("--path", type=str, required=True)
    parser.add_argument("--out_root", type=str, required=True)
    parser.add_argument("--compression", type=str, default=None)

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--concat_tokens",
        type=int,
        help="Convert text to tokens and concatenate up to this many tokens",
    )
    parser.add_argument("--split", type=str, default="train")

    parser.add_argument("--tokenizer", type=str, required=False, default=None)
    parser.add_argument("--bos_text", type=str, required=False, default=None)
    parser.add_argument("--eos_text", type=str, required=False, default=None)
    parser.add_argument("--no_wrap", default=False, action="store_true")

    # add Cond. Training specific args here
    parser.add_argument("--num-sentinels", type=int, required=False, default=None)
    # probability of using a sentinel token at any given place
    parser.add_argument("--label_prob", type=float, required=False, default=0.9)

    parsed = parser.parse_args()

    if (
        os.path.isdir(parsed.out_root)
        and len(set(os.listdir(parsed.out_root)).intersection(set(parsed.split))) > 0
    ):
        raise ValueError(
            f"--out_root={parsed.out_root} contains {os.listdir(parsed.out_root)} which cannot overlap with the requested splits {parsed.splits}."
        )

    # Make sure we have needed concat options
    if (
        parsed.concat_tokens is not None
        and isinstance(parsed.concat_tokens, int)
        and parsed.tokenizer is None
    ):
        parser.error("When setting --concat_tokens, you must specify a --tokenizer")

    # now that we have validated them, change BOS/EOS to strings
    if parsed.bos_text is None:
        parsed.bos_text = ""
    if parsed.eos_text is None:
        parsed.eos_text = ""
    return parsed


class ConcatLabeledTokensDataset(ConcatTokensDataset):
    """ """
    # subclass of ConcatTokens dataset.

    def __init__(self, *args, **kwargs):

        assert "score_to_label" in kwargs and callable(kwargs["score_to_label"])
        assert "label_prob" in kwargs

        self.score_to_label = kwargs.pop("score_to_label")
        self.label_prob = kwargs.pop("label_prob")

        super().__init__(*args, **kwargs)

    def __iter__(self) -> Iterable[Dict[str, bytes]]:

        buffer = []
        for sample in self.hf_dataset:
            assert len(sample["sentences"]) == len(sample["scores"])
            for sent, score in zip(sample["sentences"], sample["scores"]):
                encoded = self.tokenizer(sent, truncation=False, padding=False)
                label_token = self.score_to_label(score)

                if np.random.uniform() < self.label_prob:
                    iids = [label_token] + encoded["input_ids"]
                else:
                    iids = encoded["input_ids"]
                buffer = buffer + self.bos_tokens + iids + self.eos_tokens
            while len(buffer) >= self.max_length:
                concat_sample = buffer[: self.max_length]
                buffer = buffer[self.max_length :] if self.should_wrap else []
                yield {
                    # convert to bytes to store in MDS binary format
                    "tokens": np.asarray(concat_sample).tobytes()
                }


def build_hf_dataset(
    path: str,
    split: str,
    mode: ConcatMode,
    max_length: Optional[int] = None,
    bos_text: str = "",
    eos_text: str = "",
    no_wrap: bool = False,
    tokenizer: PreTrainedTokenizerBase = None,
    score_to_label: callable = None,
    label_prob: float = None,
) -> IterableDataset:
    """Build an IterableDataset over the HF C4 or pile source data.

    :param dataset_name: Dataset name
    :type dataset_name: str
    :param split: Split name.
    :type split: str
    :param mode: NO_CONCAT, or CONCAT_TOKENS
    :type mode: ConcatMode
    :param max_length: The length of concatenated tokens
    :type max_length: int
    :param bos_text: text to insert at the beginning of each sequence
    :type bos_text: str
    :param eos_text: text to insert at the end of each sequence
    :type eos_text: str
    :param no_wrap: if concatenating, whether to wrap text across `max_length` boundaries
    :type no_wrap: bool
    :param tokenizer: if mode is CONCAT_TOKENS, the tokenizer to use
    :type tokenizer: PreTrainedTokenizerBase
    :param data_subset: Referred to as "name" in HuggingFace datasets.load_dataset.
            Typically "all" (The Pile) or "en" (c4).
    :type data_subset: str
    :param path: str:
    :param split: str:
    :param mode: ConcatMode:
    :param max_length: Optional[int]:  (Default value = None)
    :param bos_text: str:  (Default value = "")
    :param eos_text: str:  (Default value = "")
    :param no_wrap: bool:  (Default value = False)
    :param tokenizer: PreTrainedTokenizerBase:  (Default value = None)
    :param score_to_label: callable:  (Default value = None)
    :param label_prob: float:  (Default value = None)
    :returns: An IterableDataset.

    """
    if os.path.isdir(path):
        data_files = glob(f"{path}/*")
    else:
        data_files = path

    hf_dataset = hf_datasets.load_dataset("json", data_files=data_files, split=split)

    if mode == ConcatMode.NO_CONCAT:
        dataset = NoConcatDataset(hf_dataset)
    else:
        if not isinstance(tokenizer, PreTrainedTokenizerBase):
            raise ValueError(f"{tokenizer=} must be of type PreTrainedTokenizerBase")
        if max_length is None:
            raise ValueError(f"max_length must be set.")
        if bos_text + eos_text == "":
            test_tokens = tokenizer("test")
            if (
                test_tokens["input_ids"][0] != tokenizer.bos_token_id
                and test_tokens["input_ids"][-1] != tokenizer.eos_token_id
            ):
                tok_error_msg = "This tokenizer does not insert an EOS nor BOS token. "
                tok_error_msg += (
                    "Concatenating with this tokenizer will result in sequences being "
                )
                tok_error_msg += "attached without a separating token. Please use another tokenizer, "
                tok_error_msg += (
                    "such as facebook/opt-125m, or specify EOS/BOS text with e.g. "
                )
                tok_error_msg += "--bos_text=<|endoftext|>."
                raise ValueError(tok_error_msg)
        dataset = ConcatLabeledTokensDataset(
            hf_dataset=hf_dataset,
            tokenizer=tokenizer,
            max_length=max_length,
            bos_text=bos_text,
            eos_text=eos_text,
            no_wrap=no_wrap,
            score_to_label=score_to_label,
            label_prob=label_prob,
        )
    return dataset


def generate_samples(
    loader: DataLoader, truncate_num_samples: Optional[int] = None
) -> Iterable[Dict[str, bytes]]:
    """Generator over samples of a dataloader.

    :param loader: A dataloader emitting batches like {key: [sample0_bytes, sample1_bytes, sample2_bytes, ...]}
    :type loader: DataLoader
    :param truncate_num_samples: An optional # of samples to stop at.
    :type truncate_num_samples: Optional[int]
    :param loader: DataLoader:
    :param truncate_num_samples: Optional[int]:  (Default value = None)

    """
    n_samples = 0
    for batch in loader:
        keys = list(batch.keys())
        current_bs = len(batch[keys[0]])
        for idx in range(current_bs):
            if truncate_num_samples is not None and n_samples == truncate_num_samples:
                return
            n_samples += 1
            yield {k: v[idx] for k, v in batch.items()}


def main(args: Namespace) -> None:
    """Main: create C4/pile streaming dataset.

    :param args: Commandline arguments.
    :type args: Namespace
    :param args: Namespace:

    """

    if args.concat_tokens is not None:
        mode = ConcatMode.CONCAT_TOKENS
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
        # we will enforce length, so suppress warnings about sequences too long for the model
        tokenizer.model_max_length = int(1e30)
        columns = {"tokens": "bytes"}
    else:
        mode = ConcatMode.NO_CONCAT
        tokenizer = None
        columns = {"text": "str"}

    # add special tokens to tokenizer, and save it

    tokenizer.add_special_tokens(
        {
            "additional_special_tokens": [
                f"<|val{x}|>" for x in range(0, args.num_sentinels)
            ]
        }
    )

    print(tokenizer.all_special_ids, tokenizer.all_special_tokens)
    print(tokenizer.decode([50277, 50278]))

    tokenizer.save_pretrained(
        f"./{args.tokenizer.replace('/', '_')}-{args.num_sentinels}-special-tokens"
    )

    # define score_to_label fn
    def score_to_label(score: float) -> int:
        """

        :param score: float:

        """
        def score_to_bucket(score: float) -> int:
            """

            :param score: float:

            """
            # simple bucketing -- bucket scores greater than cutoff into bucket1, lower into bucket0
            if score < 5.6e-4:
                return 1  # this means that toxic data is in the val0 bucket
            else:
                return 0

        bucket = score_to_bucket(score)

        return tokenizer.additional_special_tokens[bucket]

    # Get samples
    dataset = build_hf_dataset(
        path=args.path,
        split=args.split,
        mode=mode,
        max_length=args.concat_tokens,
        bos_text=args.bos_text,
        eos_text=args.eos_text,
        no_wrap=args.no_wrap,
        tokenizer=tokenizer,
        score_to_label=score_to_label,
        label_prob=args.label_prob,
    )

    # Write samples
    print(f"Converting to MDS format...")
    print(
        f"Note that the progress bar is based on the dataset length before tokenization."
    )
    print(f"It will finish at a value below 100% if tokenizing")
    with MDSWriter(
        columns=columns, out=os.path.join(args.out_root), compression=args.compression
    ) as out:
        for sample in tqdm(dataset):
            out.write(sample)


if __name__ == "__main__":
    main(parse_args())
