# Known issues from converter v1 (Phase 1 smoke tests)

These are issues observed when running `jenkins_to_gha.converter` in isolation
against the two sample Jenkinsfiles. They are exactly the kinds of
problems the Phase 2 reviewer agent + `actionlint` grounding are meant
to catch. None of them prevent `actionlint` from passing the YAML as
syntactically valid; they are semantic or pragmatic gaps.

Run that produced these outputs:

```
python -m jenkins_to_gha.converter samples/simple.Jenkinsfile > output/simple.yml
python -m jenkins_to_gha.converter samples/complex.Jenkinsfile > output/complex.yml
actionlint output/simple.yml      # clean
actionlint output/complex.yml     # clean
```

## samples/simple.Jenkinsfile -> output/simple.yml

- **Clean.** Single-run output matches the source 1:1 in intent:
  workflow-level `env: APP_ENV: ci`, single `build-and-test` job with
  checkout/build/test steps, `dorny/test-reporter@v1` with
  `if: always()` for JUnit, and `actions/upload-artifact@v4` with
  `if: always()` at the end of the job to mimic
  `post { always { archiveArtifacts ... } }`.
- **Minor**: the generated `name: CI` is a guess. The source never
  names the pipeline; reviewer may want to prefer the repo name or
  make this explicit in the prompt. Not a bug.

## samples/complex.Jenkinsfile -> output/complex.yml

### 1. Pre-test stages are collapsed into one job with a misleading name
Jenkins has three sequential top-level stages before `Tests`:
`Checkout`, `Prepare Tools`, `Build`. The converter merged them into a
single job whose id is `checkout-and-prepare-and-build` but whose
display name is just `Build`. The inner `Checkout` step also has no
explicit `name:`. Result:
- The GitHub Actions UI will show this job as `Build`, hiding the
  existence of `Prepare Tools`.
- Anyone searching logs for "Checkout" or "Prepare Tools" as step names
  will miss them.

**Reviewer fix target**: either (a) give the merged job a composite
name like `Build (checkout + tools + build)` and ensure every inner
step has `name:` preserving the original stage label, or (b) split
into three sequential jobs linked by `needs:`.

### 2. Speculative artifact upload in `build` job for files that do not exist yet
The merged `build` job uploads `build/test-results/test/*.xml` as an
artifact named `build-test-results` at the end of its steps. At that
point no tests have run - the test suite is in the downstream
`unit-tests` job. This upload will fail at runtime (no matching files)
or, worse, silently upload an empty set.

**Reviewer fix target**: remove this upload entirely. Test result
publication is already handled correctly inside `unit-tests` via
`dorny/test-reporter@v1`.

### 3. Pipeline-level `post { always }` is implemented twice
The Jenkins source has exactly one pipeline-level
`post { always { archiveArtifacts ... } }`. The converter produced:
- A step at the end of `build` uploading `build-libs`.
- An extra final `post-always` job that downloads `build-libs` and
  re-uploads as `archived-libs`.

These are functionally redundant: the artifact is already stored after
the first upload. The `post-always` job adds cost and noise without
extra safety.

**Reviewer fix target**: keep a single realization of pipeline-level
`post { always }` - either the inline upload in `build` OR a
dedicated final job, not both. A dedicated final job is the more
faithful translation because Jenkins runs the pipeline `post` block
even on failure of an intermediate stage; the inline step only runs
if `build` got far enough.

### 4. Every job runs in the Docker container, including `publish-artifacts`
This is arguably correct (Jenkins `agent { docker { ... } }` at
pipeline level applies to every stage), but running
`echo 'Publishing artifacts (placeholder)'` inside a Gradle container
is wasteful. In a real migration we would narrow the container to the
jobs that actually need it.

**Reviewer fix target**: non-blocking. Flag as an optimization, not a
correctness issue.

### 5. `lint` job downloads `build-libs` it never uses
The converter wires `needs: checkout-and-prepare-and-build` on `lint`
and adds an `actions/download-artifact@v4` for `build-libs`. `Lint`
only runs `./gradlew --no-daemon check`, which re-reads sources from
the checkout and does not consume the uploaded jars.

**Reviewer fix target**: remove the download. Minor, but reinforces
point 2 - the converter is over-eager on artifact plumbing when the
Jenkins source says nothing about cross-stage artifact flow.

### 6. `options { timestamps() }` silently dropped
Per the mapping table in `prompts/converter_system.md` this is
intentional (GitHub Actions logs are timestamped server-side). Noted
here so the reviewer does not flag it as a regression.

### 7. Stage-level `post { always { junit ... } }` is correctly mapped
Inside `Unit Tests`, Jenkins has
`post { always { junit 'build/test-results/test/*.xml' } }`. The
converter emitted `dorny/test-reporter@v1` with `if: always()` as the
last step of the `unit-tests` job. Correct.

## Summary

The converter is producing *syntactically valid, actionlint-clean*
workflows but is making over-eager assumptions about artifact flow
when `parallel {}` stages enter the picture. Issues 1, 2, 3, and 5
are exactly the semantic class the reviewer agent (Phase 2) needs to
catch, ideally with the help of `actionlint` for syntax and targeted
LLM review for these kinds of structural bugs.
