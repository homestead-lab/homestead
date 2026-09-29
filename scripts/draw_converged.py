"""Draws the README 'converged' graphics into docs/images: three options,
each showing one node and three nodes. Self-contained SVG on a dark panel
(reads on GitHub's light and dark themes), system fonts only (an <img> SVG
cannot load web fonts). Run: python scripts/draw_converged.py"""
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "images")
os.makedirs(OUT, exist_ok=True)

FONT = "'Segoe UI', system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace"
BG, PANEL, EDGE = "#101416", "#161b1e", "#283034"
CREAM, MUTED, DIM = "#F7F4EA", "#97a0a4", "#5f696e"
AMBER, BLUE, GREEN, RED = "#F8B84E", "#5AA7FF", "#3DDC91", "#FF6B6B"


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def t(x, y, s, size=15, fill=CREAM, weight=400, anchor="start", family=FONT, ls=None, opacity=None):
    extra = f' letter-spacing="{ls}"' if ls else ""
    extra += f' opacity="{opacity}"' if opacity is not None else ""
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}"{extra}>{esc(s)}</text>')


def rect(x, y, w, h, rx=0, fill="none", stroke=None, sw=1.5, fo=None, so=None, dash=None, opacity=None):
    a = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
    if fo is not None:
        a += f' fill-opacity="{fo}"'
    if stroke:
        a += f' stroke="{stroke}" stroke-width="{sw}"'
        if so is not None:
            a += f' stroke-opacity="{so}"'
    if dash:
        a += f' stroke-dasharray="{dash}"'
    if opacity is not None:
        a += f' opacity="{opacity}"'
    return a + "/>"


def path(d, stroke, sw=2, dash=None, fill="none", marker=None, opacity=None, cap="round"):
    a = f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}" stroke-linecap="{cap}" stroke-linejoin="round"'
    if dash:
        a += f' stroke-dasharray="{dash}"'
    if marker:
        a += f' marker-end="url(#{marker})"'
    if opacity is not None:
        a += f' opacity="{opacity}"'
    return a + "/>"


def svg(pfx, w, h, label, body, extra_defs=""):
    defs = (f'<marker id="{pfx}-arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M1 1 9 5 1 9z" fill="{AMBER}"/></marker>'
            f'<marker id="{pfx}-garr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M1 1 9 5 1 9z" fill="{GREEN}"/></marker>'
            f'<linearGradient id="{pfx}-sky" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#FFD166"/><stop offset="1" stop-color="#F59E0B"/></linearGradient>'
            + extra_defs)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            f'<title>{esc(label)}</title><defs>{defs}</defs>'
            f'{rect(0, 0, w, h, 24, BG)}{body}</svg>')


# The same workload in every picture.
APPS_ONE = [("Homestead", "hs"), ("Jellyfin", "app"), ("Home Assistant", "app"), ("Windows 11", "vm")]
APPS_THREE = [[("Homestead", "hs"), ("Jellyfin", "app")], [("Home Assistant", "app")], [("Windows 11", "vm")]]
VOLUMES = ["media", "config", "win11"]


def chip(x, y, w, h, name, kind, size=14):
    out = []
    if kind == "hs":
        out.append(rect(x, y, w, h, 7, AMBER))
        # the mark, small
        mx, my = x + 10, y + h / 2
        out.append(path(f"M{mx} {my - 1} {mx + 7} {my - 7} {mx + 14} {my - 1}V{my + 7}H{mx}Z", BG, 2.2))
        out.append(t(x + 32, y + h / 2 + size * 0.36, name, size, BG, 650))
    else:
        out.append(rect(x, y, w, h, 7, AMBER, AMBER, 1.3, fo=0.14, so=0.55))
        out.append(t(x + 12, y + h / 2 + size * 0.36, name, size, CREAM, 500))
        if kind == "vm":
            out.append(rect(x + w - 36, y + h / 2 - 9, 28, 18, 5, AMBER, fo=0.9))
            out.append(t(x + w - 22, y + h / 2 + 4.5, "VM", 11.5, BG, 750, "middle"))
    return "".join(out)


def replicas(x, y, w=50, h=44, gap=8, dim=False, size=12.5):
    out = []
    for i, v in enumerate(VOLUMES):
        bx = x + i * (w + gap)
        out.append(rect(bx, y, w, h, 8, GREEN, GREEN, 1.3, fo=0.2, so=0.7, opacity=0.3 if dim else None))
        out.append(t(bx + w / 2, y + h / 2 + 4.5, v, size, CREAM, 550, "middle", opacity=0.3 if dim else None))
    return "".join(out)


def vip_pill(cx, y, w=230, h=38):
    return (rect(cx - w / 2, y, w, h, h / 2, BLUE, BLUE, 1.5, fo=0.14, so=0.9)
            + f'<circle cx="{cx - w / 2 + 22}" cy="{y + h / 2}" r="5" fill="{BLUE}"/>'
            + t(cx - w / 2 + 36, y + h / 2 + 5, "VIP", 13, BLUE, 700, ls=1)
            + t(cx - w / 2 + 72, y + h / 2 + 5.5, "192.168.1.200", 16, CREAM, 600, family=MONO))


def join_arrow(pfx, x1, x2, y):
    cx = (x1 + x2) / 2
    return (path(f"M{x1} {y}H{x2}", AMBER, 2.5, marker=f"{pfx}-arr")
            + t(cx, y - 16, "add two nodes", 15, CREAM, 600, "middle")
            + t(cx, y + 28, "install.sh", 13, MUTED, 400, "middle", family=MONO)
            + t(cx, y + 46, "→ Join", 13, MUTED, 400, "middle", family=MONO))


# ---------------------------------------------------------------- A: layers
def layers():
    P, W, H = "a", 1200, 640
    NW = 200
    NET, COMP, STOR, HOST = (196, 40), (244, 148), (400, 96), (504, 36)
    TOP, BOT = 150, 552
    b = []

    def node(x, name, holder, apps, copies, standby=False):
        o = [rect(x, TOP, NW, BOT - TOP, 16, PANEL, EDGE, 1.5)]
        o.append(t(x + 16, TOP + 27, name, 16, CREAM, 650))
        o.append(f'<circle cx="{x + NW - 20}" cy="{TOP + 21}" r="5" fill="{GREEN}"/>')
        for (y, h), c in ((NET, BLUE), (COMP, AMBER), (STOR, GREEN)):
            o.append(rect(x + 8, y, NW - 16, h, 10, c, c, 1, fo=0.07, so=0.28))
        cy = NET[0] + NET[1] / 2
        if holder:
            o.append(f'<circle cx="{x + 26}" cy="{cy}" r="5.5" fill="{BLUE}"/>')
            o.append(t(x + 40, cy + 5, "holds the VIP", 14, CREAM, 500))
        else:
            o.append(f'<circle cx="{x + 26}" cy="{cy}" r="5" fill="none" stroke="{BLUE}" stroke-width="1.5"/>')
            o.append(t(x + 40, cy + 5, "standby", 14, MUTED))
        for i, (n, k) in enumerate(apps):
            o.append(chip(x + 18, COMP[0] + 10 + i * 34, NW - 36, 28, n, k))
        o.append(t(x + 18, STOR[0] + 22, "Longhorn", 13, GREEN, 650))
        o.append(t(x + NW - 18, STOR[0] + 22, copies, 13, MUTED, 400, "end"))
        o.append(replicas(x + 18, STOR[0] + 36))
        o.append(t(x + NW / 2, HOST[0] + 23, "Linux · k3s / RKE2", 13, DIM, 500, "middle", family=MONO))
        return "".join(o)

    # legend down the left, aligned with the bands
    for (y, h), label, c in ((NET, "NETWORK", BLUE), (COMP, "COMPUTE", AMBER), (STOR, "STORAGE", GREEN), (HOST, "HOST", DIM)):
        b.append(t(40, y + h / 2 + 4.5, label, 12.5, c, 700, ls=1.6))

    lx = 150
    b.append(t(lx + NW / 2, 42, "ONE NODE", 13, MUTED, 700, "middle", ls=2))
    b.append(vip_pill(lx + NW / 2, 62, 210))
    b.append(path(f"M{lx + NW / 2} 100V{TOP}", BLUE, 2))
    b.append(node(lx, "node-1", True, APPS_ONE, "1 copy each"))

    b.append(join_arrow(P, 372, 498, 330))

    xs = [520, 740, 960]
    mid = xs[1] + NW / 2
    b.append(t(mid, 42, "THREE NODES", 13, MUTED, 700, "middle", ls=2))
    b.append(vip_pill(mid, 62))
    b.append(path(f"M{mid} 100V124H{xs[0] + NW / 2}V{TOP}", BLUE, 2))
    b.append(path(f"M{mid} 124V{TOP}", BLUE, 1.5, dash="4 6", opacity=0.7))
    b.append(path(f"M{mid} 124H{xs[2] + NW / 2}V{TOP}", BLUE, 1.5, dash="4 6", opacity=0.7))
    for i, x in enumerate(xs):
        b.append(node(x, f"node-{i + 1}", i == 0, APPS_THREE[i], "3 copies each"))
    for x in xs[:2]:
        y = STOR[0] + 58
        b.append(path(f"M{x + NW - 4} {y}H{x + NW + 24}", GREEN, 2, marker=f"{P}-garr"))
        b.append(f'<circle cx="{x + NW - 4}" cy="{y}" r="2.5" fill="{GREEN}"/>')

    b.append(t(lx + NW / 2, 592, "Everything on one machine", 16, CREAM, 650, "middle"))
    b.append(t(lx + NW / 2, 615, "one copy of your data", 14, MUTED, 400, "middle"))
    b.append(t(mid, 592, "Any node can fail", 16, CREAM, 650, "middle"))
    b.append(t(mid, 615, "the VIP moves, its apps restart on another node, and every volume still has two copies", 14, MUTED, 400, "middle"))
    return svg(P, W, H, "Homestead converges compute, storage and network on every node: one node holds everything, three nodes keep a spare of each layer", "".join(b))


# ---------------------------------------------------------------- B: houses
def houses():
    P, W, H = "b", 1200, 640
    NW, PEAK, WALL, BASE = 200, 176, 252, 520
    b = []

    def house(x, name, holder, apps, copies):
        cx = x + NW / 2
        o = [path(f"M{x} {WALL}L{cx} {PEAK}L{x + NW} {WALL}V{BASE}H{x}Z", f"url(#{P}-sky)", 7, fill=PANEL)]
        # the two bars of the mark: apps on top, data below
        o.append(rect(x + 18, 266, NW - 36, 142, 12, CREAM, CREAM, 1.2, fo=0.05, so=0.35))
        o.append(t(x + 30, 287, "apps & VMs", 12.5, MUTED, 600))
        for i, (n, k) in enumerate(apps):
            o.append(chip(x + 28, 296 + i * 27, NW - 56, 23, n, k, 13))
        o.append(rect(x + 18, 418, NW - 36, 88, 12, CREAM, CREAM, 1.2, fo=0.05, so=0.35))
        o.append(t(x + 30, 439, "Longhorn", 12.5, GREEN, 650))
        o.append(t(x + NW - 30, 439, copies, 12.5, MUTED, 400, "end"))
        o.append(replicas(x + 27, 450, 44, 44, 7, size=12))
        o.append(t(cx, 552, name, 16, CREAM, 650, "middle"))
        if holder:
            o.append(f'<circle cx="{cx}" cy="{PEAK - 20}" r="6" fill="{BLUE}"/>')
            for r in (13, 21):
                o.append(path(f"M{cx - r} {PEAK - 20 - r * 0.2}A{r} {r} 0 0 1 {cx + r} {PEAK - 20 - r * 0.2}", BLUE, 2, opacity=0.8))
        else:
            o.append(f'<circle cx="{cx}" cy="{PEAK - 20}" r="5" fill="none" stroke="{BLUE}" stroke-width="1.5" opacity="0.7"/>')
        return "".join(o)

    lx = 110
    b.append(t(lx + NW / 2, 48, "ONE NODE", 13, MUTED, 700, "middle", ls=2))
    b.append(vip_pill(lx + NW / 2, 76, 210))
    b.append(path(f"M{lx + NW / 2} 114V{PEAK - 50}", BLUE, 2))
    b.append(path(f"M{lx - 30} {BASE}H{lx + NW + 30}", DIM, 2))
    b.append(house(lx, "node-1", True, APPS_ONE, "1 copy"))

    b.append(join_arrow(P, 356, 490, 380))

    xs = [520, 740, 960]
    mid = xs[1] + NW / 2
    b.append(t(mid, 48, "THREE NODES", 13, MUTED, 700, "middle", ls=2))
    b.append(vip_pill(mid, 76))
    b.append(path(f"M{mid} 114V126H{xs[0] + NW / 2}V{PEAK - 50}", BLUE, 2))
    b.append(path(f"M{mid} 126V{PEAK - 30}", BLUE, 1.5, dash="4 6", opacity=0.6))
    b.append(path(f"M{mid} 126H{xs[2] + NW / 2}V{PEAK - 30}", BLUE, 1.5, dash="4 6", opacity=0.6))
    b.append(path(f"M{xs[0] - 30} {BASE}H{xs[2] + NW + 30}", DIM, 2))
    for i, x in enumerate(xs):
        b.append(house(x, f"node-{i + 1}", i == 0, APPS_THREE[i], "3 copies"))
    for x in xs[:2]:
        y = 472
        b.append(path(f"M{x + NW - 16} {y}H{x + NW + 36}", GREEN, 2, marker=f"{P}-garr"))
        b.append(f'<circle cx="{x + NW - 16}" cy="{y}" r="2.5" fill="{GREEN}"/>')

    b.append(t(lx + NW / 2, 596, "Everything under one roof", 16, CREAM, 650, "middle"))
    b.append(t(lx + NW / 2, 619, "one copy of your data", 14, MUTED, 400, "middle"))
    b.append(t(mid, 596, "Any house can go dark", 16, CREAM, 650, "middle"))
    b.append(t(mid, 619, "the VIP moves next door, apps restart there, and every volume still has two copies", 14, MUTED, 400, "middle"))
    return svg(P, W, H, "Each Homestead node is a whole house: apps, VMs and data under one roof. Three houses share one VIP and keep three copies of every volume", "".join(b))


# ---------------------------------------------------------------- C: story
def story():
    P, W, H = "c", 1200, 500
    FW, GAP, X0 = 250, 34, 40
    NW, NG = 66, 12
    TOP, BOT = 150, 312
    b = []

    def mini(x, state, apps, vip, reps):
        """state: up | ghost | down"""
        o = []
        if state == "ghost":
            o.append(rect(x, TOP, NW, BOT - TOP, 10, "none", MUTED, 1.5, dash="5 5", so=0.6))
            o.append(t(x + NW / 2, (TOP + BOT) / 2 + 8, "+", 26, MUTED, 300, "middle"))
            return "".join(o)
        down = state == "down"
        o.append(rect(x, TOP, NW, BOT - TOP, 10, PANEL, RED if down else EDGE, 2 if down else 1.5))
        dim = 0.22 if down else None
        c = f' opacity="{dim}"' if dim else ""
        o.append(f'<circle cx="{x + NW / 2}" cy="{TOP + 16}" r="5" fill="{BLUE if vip else "none"}" stroke="{BLUE}" stroke-width="1.5"{c}/>')
        for i, k in enumerate(apps):
            y = TOP + 32 + i * 21
            if k == "hs":
                o.append(rect(x + 8, y, NW - 16, 15, 4, AMBER, opacity=dim))
            elif k == "vm":
                o.append(rect(x + 8, y, NW - 16, 15, 4, AMBER, AMBER, 1.5, fo=0.15, opacity=dim))
                o.append(t(x + NW / 2, y + 11.5, "VM", 10.5, AMBER, 750, "middle", opacity=dim))
            elif k == "new":
                o.append(rect(x + 8, y, NW - 16, 15, 4, AMBER, AMBER, 1.5, fo=0.12, so=0.9, dash="3 3"))
            elif k == "newhs":
                o.append(rect(x + 8, y, NW - 16, 15, 4, AMBER, AMBER, 1.5, fo=0.55, dash="3 3"))
            else:
                o.append(rect(x + 8, y, NW - 16, 15, 4, AMBER, fo=0.45, opacity=dim))
        for i in range(3):
            fill = reps[i] if i < len(reps) else None
            bx = x + 8 + i * 18
            if fill == "g":
                o.append(rect(bx, BOT - 26, 14, 14, 3, GREEN, opacity=dim))
            elif fill == "new":
                o.append(rect(bx, BOT - 26, 14, 14, 3, GREEN, GREEN, 1.5, fo=0.3, dash="2 2"))
        if down:
            o.append(path(f"M{x + 16} {TOP + 60}l34 34M{x + 50} {TOP + 60}l-34 34", RED, 4))
        return "".join(o)

    frames = [
        ("One node", ["up", "ghost", "ghost"], [["hs", "app", "app", "vm"], [], []], 0, [["g"] * 3, [], []],
         "apps, VMs and data in", "one box, one copy"),
        ("Three nodes", ["up"] * 3, [["hs", "app"], ["app"], ["vm"]], 0, [["g"] * 3] * 3,
         "apps spread out, every", "volume kept on 3 nodes"),
        ("node-1 fails", ["down", "up", "up"], [["hs", "app"], ["app", "newhs"], ["vm", "new"]], 1, [["g"] * 3] * 3,
         "the VIP moves, its apps", "restart on the others"),
        ("node-1 is back", ["up"] * 3, [[], ["app", "hs"], ["vm", "app"]], 1, [["new"] * 3, ["g"] * 3, ["g"] * 3],
         "its missing copies are", "rebuilt automatically"),
    ]
    for f, (title, states, apps, vip, reps, c1, c2) in enumerate(frames):
        fx = X0 + f * (FW + GAP)
        b.append(f'<circle cx="{fx + 14}" cy="{47}" r="13" fill="{AMBER}"/>')
        b.append(t(fx + 14, 52.5, str(f + 1), 15, BG, 750, "middle"))
        b.append(t(fx + 36, 53, title, 17, CREAM, 650))
        b.append(rect(fx, 76, FW, 262, 16, "#13181a", EDGE, 1.2))
        nx = [fx + (FW - (3 * NW + 2 * NG)) / 2 + i * (NW + NG) for i in range(3)]
        vx = nx[vip] + NW / 2
        b.append(rect(fx + FW / 2 - 58, 94, 116, 26, 13, BLUE, BLUE, 1.2, fo=0.14, so=0.9))
        b.append(t(fx + FW / 2, 112, ".200", 14, CREAM, 600, "middle", family=MONO))
        b.append(t(fx + FW / 2 - 44, 111.5, "VIP", 11, BLUE, 750, ls=0.8))
        b.append(path(f"M{fx + FW / 2} 120V132H{vx}V{TOP}", BLUE, 2))
        if f == 2:
            b.append(path(f"M{nx[0] + NW / 2} 132H{fx + FW / 2}", BLUE, 1.5, dash="3 4", opacity=0.35))
        for i in range(3):
            b.append(mini(nx[i], states[i], apps[i], i == vip and states[i] == "up", reps[i]))
        if f == 2:
            b.append(path(f"M{nx[0] + NW - 4} {TOP + 40}C{nx[1]} {TOP + 20} {nx[1] + 20} {TOP + 40} {nx[1] + 10} {TOP + 61}",
                          AMBER, 1.8, dash="4 4", marker=f"{P}-arr"))
            b.append(t(fx + FW / 2, 332, "2 copies left", 12.5, MUTED, 500, "middle"))
        if f == 3:
            b.append(t(fx + FW / 2, 332, "rebuilding to 3 copies", 12.5, GREEN, 600, "middle"))
        if f < 3:
            ax = fx + FW + 8
            b.append(path(f"M{ax} 207l10 10-10 10", AMBER, 2.5))
        b.append(t(fx + 4, 370, c1, 15, CREAM, 500))
        b.append(t(fx + 4, 391, c2, 15, MUTED))

    # legend
    ly = 452
    items = []
    x = X0
    b.append(path(f"M{X0} 424H{W - X0}", EDGE, 1))
    b.append(f'<circle cx="{x + 6}" cy="{ly - 5}" r="6" fill="{BLUE}"/>'); b.append(t(x + 20, ly, "holds the VIP", 14, MUTED)); x += 150
    b.append(rect(x, ly - 12, 32, 14, 4, AMBER)); b.append(t(x + 42, ly, "Homestead", 14, MUTED)); x += 140
    b.append(rect(x, ly - 12, 32, 14, 4, AMBER, fo=0.45)); b.append(t(x + 42, ly, "an app", 14, MUTED)); x += 115
    b.append(rect(x, ly - 12, 32, 14, 4, AMBER, AMBER, 1.5, fo=0.15)); b.append(t(x + 42, ly, "a VM", 14, MUTED)); x += 105
    b.append(rect(x, ly - 12, 14, 14, 3, GREEN)); b.append(t(x + 24, ly, "a copy of a volume", 14, MUTED)); x += 180
    b.append(rect(x, ly - 12, 32, 14, 4, AMBER, AMBER, 1.5, fo=0.12, dash="3 3")); b.append(t(x + 42, ly, "restarting or rebuilding", 14, MUTED))
    return svg(P, W, H, "Homestead from one node to three: add two nodes, lose one, and the VIP, apps and data carry on while the missing copies are rebuilt", "".join(b))


for name, fn in (("converged-layers", layers), ("converged-houses", houses), ("converged-story", story)):
    with open(os.path.join(OUT, name + ".svg"), "w", encoding="utf-8") as fh:
        fh.write(fn())
print("ok")
