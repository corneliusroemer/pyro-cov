# Copyright Contributors to the Pyro-Cov project.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import logging
import os
import shutil
from collections import defaultdict
from subprocess import check_call

logger = logging.getLogger(__name__)
NEXTSTRAIN_DATA = os.path.expanduser("~/github/nextstrain/nextclade/data/sars-cov-2")


def hash_sequence(seq):
    hasher = hashlib.sha1()
    hasher.update(seq.replace("\n", "").encode("utf-8"))
    return hasher.hexdigest()


class NextcladeDB:
    """
    Database to store nextclade results through time, so that only new samples
    need to be sequenced.
    """

    def __init__(self, fileprefix="results/nextcladedb", max_fasta_count=4000):
        fileprefix = os.path.realpath(fileprefix)
        self.header_filename = fileprefix + ".header.tsv"
        self.rows_filename = fileprefix + ".rows.tsv"
        self.rows_temp_filename = fileprefix + "rows.temp.tsv"
        self.fasta_filename = fileprefix + ".temp.fasta"
        self.tsv_filename = fileprefix + ".temp.tsv"
        self.output_dir = os.path.dirname(self.tsv_filename)

        # Load hashes of already-aligned sequences.
        self._already_aligned = set()
        if os.path.exists(self.rows_filename):
            with open(self.rows_filename) as f:
                for line in f:
                    key = line.split("\t", 1)[0]
                    self._already_aligned.add(key)

        self.max_fasta_count = max_fasta_count
        self._fasta_file = open(self.fasta_filename, "wt")
        self._pending = set()

        self._tasks = defaultdict(list)

    def schedule(self, sequence, *fn_args):
        """
        Schedule a task for a given input ``sequence``.
        """
        key = hash_sequence(sequence)
        if key not in self._already_aligned:
            self._schedule_alignment(key, sequence)
        self._tasks[key].append(fn_args)

    def maybe_schedule(self, sequence, *fn_args):
        """
        Schedule a task iff no new alignment work is required.
        Tasks requiring new alignment work will be silently dropped.
        """
        key = hash_sequence(sequence)
        if key in self._already_aligned:
            self._tasks[key].append(fn_args)

    def wait(self, log_every=1000):
        """
        Wait for all scheduled or maybe_scheduled tasks to complete.
        """
        self._flush()
        with open(self.header_filename) as f:
            header = f.read().strip().split("\t")
        with open(self.rows_filename) as f:
            for i, row in enumerate(f):
                row = row.strip().split("\t")
                key = row[0]
                row = dict(zip(header, row))
                for fn_args in self._tasks.pop(key, []):
                    fn, args = fn_args[0], fn_args[1:]
                    fn(*args, row)
                if log_every and i % log_every == 0:
                    print(".", end="", flush=True)

    def _schedule_alignment(self, key, sequence):
        self._fasta_file.write(">")
        self._fasta_file.write(key)
        self._fasta_file.write("\n")
        self._fasta_file.write(sequence)
        self._fasta_file.write("\n")
        self._pending.add(key)
        if len(self._pending) >= self.max_fasta_count:
            self._flush()

    def _flush(self):
        if not self._pending:
            return
        self._fasta_file.close()
        cmd = [
            "./nextclade",
            f"--input-root-seq={NEXTSTRAIN_DATA}/reference.fasta",
            "--genes=E,M,N,ORF1a,ORF1b,ORF3a,ORF6,ORF7a,ORF7b,ORF8,ORF9b,S",
            f"--input-gene-map={NEXTSTRAIN_DATA}/genemap.gff",
            f"--input-tree={NEXTSTRAIN_DATA}/tree.json",
            f"--input-qc-config={NEXTSTRAIN_DATA}/qc.json",
            f"--input-pcr-primers={NEXTSTRAIN_DATA}/primers.csv",
            f"--input-fasta={self.fasta_filename}",
            f"--output-tsv={self.tsv_filename}",
            f"--output-dir={self.output_dir}",
        ]
        logger.info(" ".join(cmd))
        check_call(cmd)

        # Append to a copy to ensure atomicity.
        if os.path.exists(self.rows_filename):
            shutil.copyfile(self.rows_filename, self.rows_temp_filename)
        with open(self.tsv_filename) as f:
            with open(self.rows_temp_filename, "a") as frows:
                for i, line in enumerate(f):
                    if i:
                        frows.write(line)
                    else:
                        with open(self.header_filename, "w") as fheader:
                            fheader.write(line)
        os.rename(self.rows_temp_filename, self.rows_filename)
        os.remove(self.fasta_filename)
        os.remove(self.tsv_filename)
        self._fasta_file = open(self.fasta_filename, "w")
        self._already_aligned.update(self._pending)
        self._pending.clear()


class ShardedFastaWriter:
    """
    Writer that splits into multiple fasta files to avoid nextclade file size
    limit.
    """

    def __init__(self, filepattern, max_count=5000):
        assert filepattern.count("*") == 1
        self.filepattern = filepattern
        self.max_count = max_count
        self._file_count = 0
        self._line_count = 0
        self._file = None

    def _open(self):
        filename = self.filepattern.replace("*", str(self._file_count))
        print(f"writing to {filename}")
        return open(filename, "wt")

    def __enter__(self):
        assert self._file is None
        self._file = self._open()
        self._file_count += 1
        return self

    def __exit__(self, *args, **kwargs):
        self._file.close()
        self._file = None
        self._file_count = 0
        self._line_count = 0

    def write(self, name, sequence):
        if self._line_count == self.max_count:
            self._file.close()
            self._file = self._open()
            self._file_count += 1
            self._line_count = 0
        self._file.write(">")
        self._file.write(name)
        self._file.write("\n")
        self._file.write(sequence)
        self._file.write("\n")
        self._line_count += 1
