"""One-shot generator for docs/solana-copilot-architecture.excalidraw. Not imported by the server."""
from __future__ import annotations

import json
import random
from pathlib import Path

rng = random.Random(42)
NOW = 1_728_000_000_000
seq = 0
elements: list[dict] = []


def nid() -> str:
    global seq
    seq += 1
    return f"el{seq:04d}{rng.randint(1000, 9999)}"


def base(**kw):
    el = {
        "id": nid(),
        "x": 0,
        "y": 0,
        "width": 100,
        "height": 40,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": rng.randint(1, 2**31),
        "version": 1,
        "versionNonce": rng.randint(1, 2**31),
        "isDeleted": False,
        "boundElements": None,
        "updated": NOW,
        "link": None,
        "locked": False,
    }
    el.update(kw)
    elements.append(el)
    return el


def bind(el, item):
    el["boundElements"] = (el["boundElements"] or []) + [item]


def box(x, y, w, h, fill, *, stroke="#1e1e1e", sw=2, roundness=3, frame=None, dashed=False):
    return base(
        type="rectangle",
        x=x,
        y=y,
        width=w,
        height=h,
        backgroundColor=fill,
        strokeColor=stroke,
        strokeWidth=sw,
        strokeStyle="dashed" if dashed else "solid",
        roundness={"type": roundness} if roundness else None,
        frameId=frame,
    )


def rect(x, y, w, h, fill, label, *, stroke="#1e1e1e", sw=2, roundness=3, fs=18, frame=None, dashed=False):
    r = base(
        type="rectangle",
        x=x,
        y=y,
        width=w,
        height=h,
        backgroundColor=fill,
        strokeColor=stroke,
        strokeWidth=sw,
        strokeStyle="dashed" if dashed else "solid",
        roundness={"type": roundness} if roundness else None,
        frameId=frame,
    )
    t = text(x, y, w, h, label, fs=fs, frame=frame, container=r["id"])
    bind(r, {"id": t["id"], "type": "text"})
    return r


def text(x, y, w, h, s, *, fs=18, frame=None, container=None, align="center", valign="middle", color="#1e1e1e"):
    lines = s.count("\n") + 1
    return base(
        type="text",
        x=x,
        y=y,
        width=w,
        height=h,
        text=s,
        originalText=s,
        fontSize=fs,
        fontFamily=1,
        textAlign=align,
        verticalAlign=valign,
        containerId=container,
        lineHeight=1.25,
        autoResize=container is None,
        strokeColor=color,
        backgroundColor="transparent",
        roundness=None,
        frameId=frame,
        baseline=int(fs * 0.9 * lines),
    )


def arrow(a, b, label=None, *, color="#1e1e1e", dashed=False, start=None, end="arrow"):
    ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
    bx, by = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    # leave boxes from nearest edges
    def edge(box, toward_x, toward_y):
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dx, dy = toward_x - cx, toward_y - cy
        if abs(dx) * box["height"] > abs(dy) * box["width"]:
            sx = box["x"] + (box["width"] if dx > 0 else 0)
            sy = cy
        else:
            sx = cx
            sy = box["y"] + (box["height"] if dy > 0 else 0)
        return sx, sy

    x1, y1 = edge(a, bx, by)
    x2, y2 = edge(b, ax, ay)
    arr = base(
        type="arrow",
        x=x1,
        y=y1,
        width=x2 - x1,
        height=y2 - y1,
        points=[[0, 0], [x2 - x1, y2 - y1]],
        startBinding={"elementId": a["id"], "focus": 0, "gap": 6},
        endBinding={"elementId": b["id"], "focus": 0, "gap": 6},
        startArrowhead=start,
        endArrowhead=end,
        strokeColor=color,
        strokeStyle="dashed" if dashed else "solid",
        strokeWidth=2,
        roundness={"type": 2},
        elbowed=False,
    )
    bind(a, {"id": arr["id"], "type": "arrow"})
    bind(b, {"id": arr["id"], "type": "arrow"})
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        text(mx - 70, my - 22, 140, 28, label, fs=12, color=color)
    return arr


def frame(x, y, w, h, name, fill="#f8f9fa"):
    return base(
        type="frame",
        x=x,
        y=y,
        width=w,
        height=h,
        name=name,
        backgroundColor=fill,
        strokeColor="#868e96",
        strokeWidth=1,
        roundness=None,
    )


# ── Title ────────────────────────────────────────────────────────────────────
text(80, 40, 1400, 48, "Vanna Solana Copilot  —  architecture", fs=36, align="left")
text(
    80,
    92,
    1600,
    28,
    "Same split as Stellar: Gemini understands language. MCP builds unsigned txs. Wallet signs. Chain confirms.",
    fs=16,
    align="left",
    color="#495057",
)

# ── Three rules ──────────────────────────────────────────────────────────────
r1 = rect(80, 140, 420, 72, "#d3f9d8", "1. AI = language only\nCode decides amounts and risk", fs=15)
r2 = rect(520, 140, 420, 72, "#ffe8cc", "2. MCP builds, never signs\nWallet is the only signer", fs=15)
r3 = rect(960, 140, 460, 72, "#ffc9c9", "3. Done = confirmed on chain\nNot built. Not signed. Confirmed.", fs=15)

# ── Frame A: runtime path ────────────────────────────────────────────────────
fa = frame(60, 250, 2120, 720, "A. Runtime  —  one prompt")
FID = fa["id"]

def R(x, y, w, h, fill, label, **kw):
    return rect(x, y, w, h, fill, label, frame=FID, **kw)

user = R(100, 430, 200, 100, "#a5d8ff", "User\n(browser)", fs=18)
wallet = R(100, 560, 200, 90, "#74c0fc", "Wallet\nPhantom / Backpack", fs=16)
ui = R(380, 430, 230, 100, "#b2f2bb", "Copilot UI\n/copilot  chat + card", fs=16)
brain = R(690, 410, 280, 140, "#8ce99a", "Copilot brain\nhandleChat  (app server)", fs=17, sw=3)
gemini = R(720, 300, 220, 80, "#d0bfff", "Gemini 3.8 Flash\nVertex  ·  language", fs=15)
mcp = R(1060, 410, 260, 140, "#ffd8a8", "Vanna Solana MCP\n14 tools  ·  never signs", fs=16, sw=3)
fork = R(1420, 410, 280, 140, "#ffa8a8", "Surfpool fork\nvanna_lending  H37YVBG3…", fs=16, sw=3)
store = R(690, 590, 280, 90, "#e9fac8", "Plan store\nFirestore  (solana-mcp)", fs=15)
sol = R(1060, 590, 260, 90, "#ffe8cc", "lib/solana\nbroadcast + confirm", fs=15)
keeper = R(1420, 590, 280, 90, "#ffd8a8", "Price keeper\n/api/prices/refresh", fs=15)
workos = R(380, 590, 230, 90, "#fff3bf", "WorkOS M2M\nCopilot → MCP token", fs=15)

arrow(user, ui, "prompt")
arrow(ui, brain, "POST /api/copilot")
arrow(brain, gemini, "intent / wording", dashed=True, color="#7048e8")
arrow(brain, mcp, "tools/call")
arrow(mcp, fork, "read + simulate")
arrow(mcp, keeper, "before risky build", dashed=True)
arrow(brain, store, "freeze plan")
arrow(brain, ui, "answer / preview")
arrow(ui, wallet, "please sign")
arrow(wallet, ui, "signed tx")
arrow(ui, sol, "submit")
arrow(sol, fork, "send + confirm")
arrow(brain, workos, "mint bearer", dashed=True, color="#e67700")
arrow(workos, mcp, "Authorization", dashed=True, color="#e67700")

text(
    100,
    700,
    1960,
    240,
    "Brain order (cannot skip):  session  →  rate limit  →  firewall  →  keyword router (Gemini only if keywords miss)  →  lane\n"
    "Lanes: refuse  ·  ask (missing detail)  ·  read  ·  write (one step)  ·  chain (≤8 steps)  ·  lever\n\n"
    "Read  —  “What's my health factor?”  MCP vanna_account.health → Gemini may reword numbers, never invent them.\n"
    "Write —  “Supply 100 USDC”  MCP builds unsigned v0 tx + simulates → preview → wallet signs → lib/solana confirms.\n"
    "Plan  —  “Lever my SOL 2x”  server stores digest-bound steps (5 min). Each step: claim-once → fresh 1.15 check → rebuild → sign → confirm.",
    fs=15,
    align="left",
    valign="top",
    color="#343a40",
)

# ── Frame B: hosting ─────────────────────────────────────────────────────────
fb = frame(60, 1010, 2120, 520, "B. Hosting  —  staging, live 2026-10-02  (MCP over HTTPS: Cloud Run IAM + WorkOS)")
BID = fb["id"]

def H(x, y, w, h, fill, label, **kw):
    kw.pop("frame", None)
    return rect(x, y, w, h, fill, label, frame=BID, **kw)

app_p = box(100, 1080, 920, 400, "#e7f5ff", sw=1, frame=BID)
text(120, 1096, 880, 36, "GCP  vanna-credit-solana-test    us-central1", fs=20, align="left", frame=BID)
app_run = H(140, 1150, 400, 120, "#b2f2bb", "Cloud Run  vanna-credit-solana-test\nhttps://test.solana.vanna.finance", fs=15, frame=BID)
app_sa = H(140, 1290, 400, 80, "#d3f9d8", "runtime SA\nvanna-credit-runtime@…", fs=14, frame=BID)
app_vpc = H(580, 1150, 400, 220, "#c5f6fa", "VPC  vanna-internal\nsubnet  10.50.0.0/24\nDirect VPC egress (all-traffic)\n+ Cloud NAT  (router vanna-internal-router)", fs=15, frame=BID)
app_sec = H(140, 1390, 400, 70, "#fff3bf", "Secret Manager\nworkos-m2m-client-secret · session · store", fs=13, frame=BID)
app_net = H(580, 1390, 400, 70, "#e7f5ff", "Internet via NAT\nJupiter · fork RPC · WorkOS token URL", fs=13, frame=BID)

mcp_p = box(1220, 1080, 900, 400, "#fff4e6", sw=1, frame=BID)
text(1240, 1096, 860, 36, "GCP  solana-mcp    us-central1", fs=20, align="left", frame=BID)
mcp_run = H(1260, 1150, 380, 100, "#ffd8a8", "Cloud Run  vanna-mcp-solana\ningress = all  ·  IAM required (invoker = app SA)\nAUTH_ENABLED=true  (WorkOS token checked)", fs=14, frame=BID)
fsstore = H(1680, 1150, 400, 100, "#e9fac8", "Firestore  Native  (default)\nplans · limits · sessions", fs=15, frame=BID)
vert = H(1260, 1280, 380, 80, "#d0bfff", "Vertex  gemini-3.8-flash", fs=15, frame=BID)
mcp_vpc = H(1680, 1280, 400, 80, "#c5f6fa", "VPC  vanna-internal  10.40.0.0/24\npeered with the app VPC — NOT on the MCP call path", fs=13, frame=BID)
workos_h = H(1260, 1390, 380, 70, "#fff3bf", "WorkOS AuthKit  (staging)\nM2M app 'Vanna Solana Copilot'  ·  JWKS", fs=13, frame=BID)

# Internal ingress does not accept a caller from another project, so the MCP is reached over its
# run.app URL. Two tokens: Google ID token (X-Serverless-Authorization) + WorkOS bearer (Authorization).
arrow(app_vpc, mcp_run, "HTTPS run.app\nGoogle ID token + WorkOS bearer", color="#0c8599")
arrow(app_vpc, mcp_vpc, "peered (unused for MCP)", dashed=True, color="#adb5bd")
arrow(app_run, app_vpc, "egress", dashed=True)
arrow(app_run, app_sec, "mounted env", dashed=True)
arrow(app_vpc, app_net, "NAT", dashed=True)
arrow(mcp_run, workos_h, "verify JWT", dashed=True, color="#e67700")
arrow(mcp_run, fsstore, "", dashed=True)
arrow(app_run, vert, "Gemini", dashed=True, color="#7048e8")

# ── Legend ───────────────────────────────────────────────────────────────────
text(60, 1560, 400, 28, "Colour key", fs=18, align="left")
rect(60, 1600, 180, 44, "#a5d8ff", "User / wallet", fs=13)
rect(260, 1600, 180, 44, "#8ce99a", "Copilot code", fs=13)
rect(460, 1600, 180, 44, "#d0bfff", "Gemini", fs=13)
rect(660, 1600, 180, 44, "#ffd8a8", "MCP / keeper", fs=13)
rect(860, 1600, 180, 44, "#ffa8a8", "Chain", fs=13)
rect(1060, 1600, 200, 44, "#fff3bf", "WorkOS identity", fs=13)
rect(1280, 1600, 240, 44, "#c5f6fa", "VPC / hosting", fs=13)

text(
    60,
    1660,
    2000,
    80,
    "Not on this board (v1): Sign Service, auto-sign, Hermes, admin tools, liquidation execution, mainnet.\n"
    "Solana vs Stellar: same layers. Different signing (wallet only), login (SIWS), venues (Kamino + Jupiter), fork prices (keeper).",
    fs=15,
    align="left",
    color="#495057",
)

doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": elements,
    "appState": {
        "gridSize": None,
        "viewBackgroundColor": "#ffffff",
        "currentItemFontFamily": 1,
    },
    "files": {},
}

out = Path(__file__).with_name("solana-copilot-architecture.excalidraw")
out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
print(f"wrote {out}  elements={len(elements)}")
