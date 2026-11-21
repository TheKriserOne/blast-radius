IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check coverage imports

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

coverage:
	docker build --platform linux/amd64 --target coverage --tag $(IMAGE)-coverage .
	docker run --rm --platform linux/amd64 $(IMAGE)-coverage cat /coverage.json.gz > coverage.json.gz.tmp
	mv coverage.json.gz.tmp coverage.json.gz

imports:
	docker build --platform linux/amd64 --target imports --tag $(IMAGE)-imports .
	docker run --rm --platform linux/amd64 $(IMAGE)-imports cat /import_map.json.gz > import_map.json.gz.tmp
	mv import_map.json.gz.tmp import_map.json.gz

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"
