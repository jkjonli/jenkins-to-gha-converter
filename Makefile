# Makefile — convenience targets for the Jenkins → GitHub Actions converter.
#
# Each conversion target archives any existing output for that sample into
# output/archive/<UTC-timestamp>/ BEFORE running the pipeline, so previous
# transcripts and generated workflows are preserved.
#
# A single `make` invocation shares one TIMESTAMP, so `make demo` archives
# both samples under the same snapshot directory.

PYTHON          ?= .venv/bin/python
CONVERTER_MODEL ?= claude-opus-4-7
REVIEWER_MODEL  ?= claude-opus-4-7
MAX_ITERATIONS  ?= 5

SAMPLES_DIR := samples
OUTPUT_DIR  := output
ARCHIVE_DIR := $(OUTPUT_DIR)/archive

# UTC timestamp evaluated once per `make` invocation.
TIMESTAMP := $(shell date -u +%Y%m%dT%H%M%SZ)

PIPELINE := $(PYTHON) -m jenkins_to_gha.pipeline
COMMON_FLAGS := \
	--max-iterations $(MAX_ITERATIONS) \
	--converter-model $(CONVERTER_MODEL) \
	--reviewer-model $(REVIEWER_MODEL)

.DEFAULT_GOAL := help

.PHONY: help \
        convert-simple convert-complex \
        convert-simple-less-than-ideal convert-complex-less-than-ideal \
        demo test clean-archive

help:
	@echo "Available targets:"
	@echo "  convert-simple                    Convert samples/simple.Jenkinsfile"
	@echo "  convert-complex                   Convert samples/complex.Jenkinsfile"
	@echo "  convert-simple-less-than-ideal    Convert simple sample with --less-than-ideal demo mode"
	@echo "  convert-complex-less-than-ideal   Convert complex sample with --less-than-ideal demo mode"
	@echo "  demo                              Run both samples with --less-than-ideal end-to-end"
	@echo "  test                              Run the unit-test suite"
	@echo "  clean-archive                     Delete $(ARCHIVE_DIR)/"
	@echo
	@echo "Variables (override on the command line, e.g. MAX_ITERATIONS=3):"
	@echo "  PYTHON           default: $(PYTHON)"
	@echo "  CONVERTER_MODEL  default: $(CONVERTER_MODEL)"
	@echo "  REVIEWER_MODEL   default: $(REVIEWER_MODEL)"
	@echo "  MAX_ITERATIONS   default: $(MAX_ITERATIONS)"
	@echo
	@echo "Pre-existing $(OUTPUT_DIR)/<sample>.yml and .transcript.md are copied"
	@echo "to $(ARCHIVE_DIR)/<UTC-timestamp>/ before each conversion run."

# --- archive helper -----------------------------------------------------
# $(call archive,<basename>)
# Copies output/<basename>.yml and output/<basename>.transcript.md into
# the per-run archive subdir, if they exist. Skipped silently otherwise.
define archive
	@mkdir -p "$(ARCHIVE_DIR)/$(TIMESTAMP)"
	@for ext in yml transcript.md; do \
		src="$(OUTPUT_DIR)/$(1).$$ext"; \
		if [ -f "$$src" ]; then \
			cp -p "$$src" "$(ARCHIVE_DIR)/$(TIMESTAMP)/"; \
			echo "archived $$src -> $(ARCHIVE_DIR)/$(TIMESTAMP)/"; \
		fi; \
	done
endef

# --- conversion targets -------------------------------------------------

convert-simple:
	$(call archive,simple)
	$(PIPELINE) $(SAMPLES_DIR)/simple.Jenkinsfile $(OUTPUT_DIR)/simple.yml $(COMMON_FLAGS)

convert-complex:
	$(call archive,complex)
	$(PIPELINE) $(SAMPLES_DIR)/complex.Jenkinsfile $(OUTPUT_DIR)/complex.yml $(COMMON_FLAGS)

convert-simple-less-than-ideal:
	$(call archive,simple)
	$(PIPELINE) $(SAMPLES_DIR)/simple.Jenkinsfile $(OUTPUT_DIR)/simple.yml $(COMMON_FLAGS) --less-than-ideal

convert-complex-less-than-ideal:
	$(call archive,complex)
	$(PIPELINE) $(SAMPLES_DIR)/complex.Jenkinsfile $(OUTPUT_DIR)/complex.yml $(COMMON_FLAGS) --less-than-ideal

# `demo` runs both samples in --less-than-ideal mode end-to-end. Make's
# default-on-error behaviour applies: if the simple run fails, the complex
# run is not attempted.
demo: convert-simple-less-than-ideal convert-complex-less-than-ideal
	@echo
	@echo "demo complete. Snapshot of pre-run outputs at: $(ARCHIVE_DIR)/$(TIMESTAMP)/"

# --- housekeeping -------------------------------------------------------

test:
	$(PYTHON) -m unittest discover tests

clean-archive:
	rm -rf "$(ARCHIVE_DIR)"
