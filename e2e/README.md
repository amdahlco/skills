# End-to-end fixture

`scan-check/SKILL.md` is a harmless skill the Amdahl team submits to
amdahl.ai/skills to test the whole path: the format check on submit, the
code that confirms the submission, the two scanners and the AI security
review in `scan-queue.yml`, and the result on the submission's private
status link.

Skills that pass every check publish automatically, so a clean run lists
this skill. After the test, its author takes it down (unpublish), which
needs a code sent to the author's email like any other change.
