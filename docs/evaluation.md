# Golden-Set Evaluation

> Back to the [README](../README.md)

A *golden set* is a set of real remittance messages whose expected output a person has checked. It is how you measure accuracy and catch regressions when the code, the Content Understanding analyzer, the agent instructions or a model version changes.

Keep the golden set in `samples/golden/`, which is git-ignored: **real remittances must never be committed**.

## What it scores

[scripts/golden_eval.py](../scripts/golden_eval.py) runs every sample through the pipeline and scores:

- payment lines: precision and recall on reference + amount, and dollar coverage;
- the run's details (payer, date, total, reference);
- the status (complete or review) and the triage issues raised.

Each run writes a report and compares it with the previous one, so a regression shows up as a change, not just a lower score.

## Commands

```powershell
python scripts/golden_eval.py add <file.msg> --id <name> --cu live --resource-group <resource-group>   # draft labels from the current output
python scripts/golden_eval.py verify <name> --by "<reviewer>"                                       # after a person has checked them
python scripts/golden_eval.py run                                     # local pipeline, cached service responses: fast, no cost
python scripts/golden_eval.py run --cu refresh                        # after an analyzer or model change
python scripts/golden_eval.py run --mode api --api https://<ui-fqdn>  # the deployed pipeline, end to end
```

## How to use it

- **Draft, then verify.** `add` copies the current output as the expected result, so a new sample passes by definition until a person corrects and verifies its labels. Only verified samples fail the run (exit code 1).
- **Cached vs live.** The default run replays cached Content Understanding and agent responses: it shows the effect of a code change in seconds and at no cost, but not a service answering differently for the same input. Run `--cu refresh` or `--mode api` periodically and after any analyzer, model or agent update.
- **Hold some back.** Keep part of the set out of day-to-day development and use it only to report accuracy, so the numbers reflect formats the solution hasn't been tuned on.

Label format: [schemas/golden-sample-v1.json](../schemas/golden-sample-v1.json).
