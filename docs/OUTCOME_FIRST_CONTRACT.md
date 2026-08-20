# Outcome-first qualification contract

Every qualification run owns one concrete mission slice with a written stopping
condition. It gets one model call, one isolated stage, and one bounded inference
and test timeout. There is no automatic retry loop.

The trial must diagnose a real defect, return one to four scoped edits, and pass
the smallest authoritative project test that can disprove the fix. Verifier-only
files may be copied into the stage with `--verify-context`; their contents are
not sent to the model. The source workspace is never edited by `local-edit`.

Success means the requested outcome is present in the staged diff and the real
test command passes. A health prompt, schema acceptance, test count, or vendor
benchmark is not promotion evidence.

If the single call fails, times out, loops, emits a malformed contract, or cannot
pass the verifier, stop the trial and hand off the diagnosis. Do not build more
infrastructure or repeat variants inside the same slice. Follow-up ideas belong
in the next explicitly chosen slice.
