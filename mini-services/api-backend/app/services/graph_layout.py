def _layout(nodes: list[dict], edges: list[dict]) -> None:
    """v108: assign non-overlapping canvas positions in place.

    Both AI builders composed graphs with every node hardcoded to
    position (0, 0) - structurally correct pipelines that were
    functionally invisible in the workflow editor (every node stacked
    on the exact same point, reading as a single blank canvas). This
    does a simple layered (BFS-depth) layout from the graph's root
    node(s) - trigger(s) at depth 0, each hop right by 280px, siblings
    at the same depth stacked 160px apart and centered - so a composed
    graph opens already readable, the way a hand-built one would.
    """
    ids = [n["id"] for n in nodes]
    children: dict[str, list[str]] = {i: [] for i in ids}
    has_incoming: set[str] = set()
    for e in edges:
        if e.get("source") in children:
            children[e["source"]].append(e.get("target"))
        if e.get("target") in ids:
            has_incoming.add(e["target"])

    roots = [i for i in ids if i not in has_incoming] or ids[:1]
    depth: dict[str, int] = {}
    order: list[str] = []
    frontier = list(roots)
    for r in roots:
        depth[r] = 0
    seen = set(roots)
    while frontier:
        nxt = []
        for nid in frontier:
            order.append(nid)
            for c in children.get(nid, []):
                if c not in seen:
                    seen.add(c)
                    depth[c] = depth[nid] + 1
                    nxt.append(c)
        frontier = nxt
    # anything unreached (disconnected) still gets a slot, one per depth
    for i in ids:
        if i not in depth:
            depth[i] = max(depth.values(), default=-1) + 1
            order.append(i)

    by_depth: dict[int, list[str]] = {}
    for i in order:
        by_depth.setdefault(depth[i], []).append(i)

    pos = {}
    for d, level in by_depth.items():
        n = len(level)
        for k, nid in enumerate(level):
            pos[nid] = (d * 280, (k - (n - 1) / 2) * 160)

    for node in nodes:
        x, y = pos.get(node["id"], (0, 0))
        node["position"] = {"x": x, "y": y}
