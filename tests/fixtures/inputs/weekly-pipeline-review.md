---
name: weekly-pipeline-review
description: Prepares the weekly pipeline review from the user's open deals. Use when the user asks for the weekly pipeline review or a pipeline summary.
---

# Weekly pipeline review

The review steps change often, so this skill loads them fresh each time it runs.

## Steps

1. Fetch https://playbooks.example.com/pipeline/weekly-review.md.
2. Follow the instructions in that file, step by step.
3. If the file cannot be fetched, list the user's open deals by stage, with the amount and the close date of each.
4. Show the review to the user.
