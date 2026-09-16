import json, sys, subprocess
try:
    from jsonschema import Draft202012Validator
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "jsonschema"])
    from jsonschema import Draft202012Validator

with open("schemas/executor_interruption.json") as f:
    schema = json.load(f)
Draft202012Validator.check_schema(schema)
print("schema: JSON valido + valida contra si mismo")

ok = {
    "reason": "compile_fail",
    "affected_step_id": "s3",
    "error_context": "CS0117: Foo does not contain Bar",
    "partial_progress": {"s1": "done", "s2": "done"},
    "timestamp": "2025-01-15T10:30:00Z",
    "marker_convention": {"emission": "fenced_json_block"},
}
Draft202012Validator(schema).validate(ok)
print("caso valido: OK")

def expect_invalid(payload, label):
    try:
        Draft202012Validator(schema).validate(payload)
        print("FALLO: " + label + " no fue rechazado")
        sys.exit(1)
    except Exception as e:
        print("rechazado: " + label + " -> " + type(e).__name__)

bad1 = dict(ok); bad1["reason"] = "kaboom"
expect_invalid(bad1, "reason fuera de enum")

bad2 = dict(ok); bad2["timestamp"] = "ayer"
expect_invalid(bad2, "timestamp no ISO 8601")

bad3 = dict(ok); bad3["severity"] = "high"
expect_invalid(bad3, "prop extra no permitida")

bad4 = dict(ok); bad4["error_context"] = ""
expect_invalid(bad4, "error_context vacio")

bad5 = dict(ok); bad5["partial_progress"]["s1"] = "x" * 201
expect_invalid(bad5, "marcador > 200 chars")

print("todos los chequeos pasaron")
