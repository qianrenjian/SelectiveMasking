#! /bin/bash

# Download books
mkdir -p ./download
python3 $HOME/bookcorpus/download_files.py --list $HOME/bookcorpus/url_list.jsonl --out ./download --trash-bad-count

# Clean and prep (one book per line)
python3 ./clean_and_merge_text.py ./download bookcorpus.txt

