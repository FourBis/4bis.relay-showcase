import json, sys, jsonschema
schema = json.load(open("schemas/verifier_verdict.json"))
jsonschema.Draft202012Validator.check_schema(schema)
print("schema OK")
v = jsonschema.Draft202012Validator(schema)
for i, ex in enumerate(schema.get("examples", [])):
    errs = list(v.iter_errors(ex))
    print(f"example {i}: {len(errs)} errors")
    for e in errs:
        print("  -", e.message)
        sys.exit(1)
# Casos negativos que el contrato DEBE rechazar
bad = [
    {"verdict": "needs_human", "feedback": "x", "steps_completed": []},       # enum (debe fallar)
    {"verdict": "needs_more", "feedback": "x", "steps_completed": []},       # falta plan_correction
    {"verdict": "complete", "feedback": "x", "steps_completed": [], "plan_correction": {"revised_steps": [], "feedback_to_executor": "a", "remove_step_ids": [], "add_step_ids": []}},  # revised_steps vacío
    {"verdict": "complete", "feedback": "", "steps_completed": []},          # feedback vacío
    {"verdict": "complete", "feedback": "x", "steps_completed": [], "extra": 1},  # additionalProperties
    {"verdict": "off_plan", "feedback": "x", "steps_completed": ["1"], "plan_correction": {"revised_steps": [{"id":"1","description":"a","expected_output":"b","status":"done"}], "feedback_to_executor": "c", "remove_step_ids": [], "add_step_ids": []}},
    {"verdict": "needs_more", "feedback": "x", "steps_completed": [], "plan_correction": {"revised_steps": [{"id":"bad id","description":"a","expected_output":"b","status":"done"}], "feedback_to_executor": "c", "remove_step_ids": [], "add_step_ids": []}},
    {"verdict": "complete", "feedback": "x", "steps_completed": []},
]
# Casos 5 y 7 son válidos: el esquema los acepta correctamente.
expected_rejects = {0, 1, 2, 3, 4, 6}
for i, b in enumerate(bad):
    errs = list(v.iter_errors(b))
    expected_reject = i in expected_rejects
    actually_rejects = len(errs) > 0
    ok = actually_rejects == expected_reject
    print(f"neg {i}: {len(errs)} errors (expected_reject={expected_reject}) -> {'OK' if ok else 'FAIL'}")
    if not ok:
        for e in errs:
            print("  err:", e.message)
        sys.exit(1)
print("all checks passed")