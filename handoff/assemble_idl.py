import json
import re
from pathlib import Path

S = Path("C:/WINDOWS/TEMP/claude/D--Projects-Vanna-MCP-Solana/fe56177b-394d-43b8-a7f0-ba2333689a73/scratchpad")
OLD = Path("D:/Projects/Vanna_MCP_Solana/idl/vanna_lending.json")
text = (S / "idl_build.log").read_text(encoding="utf-8", errors="replace")

sections = re.findall(r"--- IDL begin (\w+) ---\s*(.*?)\s*--- IDL end \1 ---", text, flags=re.S)
by_kind: dict[str, list] = {}
for kind, body in sections:
    by_kind.setdefault(kind, []).append(json.loads(body))

program = by_kind["program"][0]


def short(name: str) -> str:
    return name.split("::")[-1]


def shorten(node):
    """Recursively shorten fully-qualified `name` values and `defined` references."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "name" and isinstance(value, str):
                out[key] = short(value)
            elif key == "defined" and isinstance(value, dict):
                out[key] = {**value, "name": short(value["name"])}
            else:
                out[key] = shorten(value)
        return out
    if isinstance(node, list):
        return [shorten(x) for x in node]
    return node


types = list(program.get("types", []))
events = []
for entry in by_kind["event"]:
    events.append(entry["event"])
    types.extend(entry["types"])

seen, uniq_types = set(), []
for t in types:
    key = t["name"]
    if key not in seen:
        seen.add(key)
        uniq_types.append(t)

idl = {
    "address": program["address"],
    "metadata": program["metadata"],
    "instructions": shorten(program["instructions"]),
    "accounts": shorten(program["accounts"]),
    "events": shorten(events),
    "errors": by_kind["errors"][0],
    "types": sorted(shorten(uniq_types), key=lambda t: t["name"]),
    "constants": by_kind["const"],
}
(S / "idl_new.json").write_text(json.dumps(idl, indent=2) + "\n", encoding="utf-8")

# --- validate the assembly against the old IDL: unchanged instructions must match exactly
old = json.loads(OLD.read_text(encoding="utf-8"))
print("old top-level keys:", list(old))
print("new top-level keys:", list(idl))
old_i = {i["name"]: i for i in old["instructions"]}
new_i = {i["name"]: i for i in idl["instructions"]}
print("instr added:", sorted(set(new_i) - set(old_i)), "removed:", sorted(set(old_i) - set(new_i)))
same, changed = [], []
for name in old_i:
    if name in new_i:
        (same if json.dumps(old_i[name], sort_keys=True) == json.dumps(new_i[name], sort_keys=True) else changed).append(name)
print("identical instructions:", len(same), "changed:", changed)
ot = {t["name"]: t for t in old["types"]}
nt = {t["name"]: t for t in idl["types"]}
print("types added:", sorted(set(nt) - set(ot)), "removed:", sorted(set(ot) - set(nt)))
print("types changed:", [n for n in ot if n in nt and json.dumps(ot[n], sort_keys=True) != json.dumps(nt[n], sort_keys=True)])
print("events added:", sorted({e['name'] for e in idl['events']} - {e['name'] for e in old['events']}),
      "removed:", sorted({e['name'] for e in old['events']} - {e['name'] for e in idl['events']}))
print("accounts new:", [a["name"] for a in idl["accounts"]], "sample:", json.dumps(idl["accounts"][0]))
