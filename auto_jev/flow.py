"""Dependency-graph compilation for pipeline specs.

Version 1 specs keep their serial semantics and compile to a prior-node chain.
Version 2 specs compile to a DAG: a node's effective dependencies are its
explicit ``depends_on`` list unioned with the ``nodes['<id>']`` references
statically inferred from its expression (or ``state`` expression for jev nodes).

This module must not import ``spec.py`` (``spec.validate_spec`` calls
``compile_flow``) and it never mutates the spec it receives.
"""
import ast

__all__ = ['expression_dependencies', 'compile_flow', 'topological_order', 'descendants']

NODES_NAME = 'nodes'
_INDEX = getattr(ast, 'Index', None)  # Python < 3.9 wraps subscript constants in ast.Index


def _literal_key(slice_node):
    """Return the literal string key of a subscript slice, or None when it is dynamic."""
    if _INDEX is not None and isinstance(slice_node, _INDEX):
        slice_node = slice_node.value
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def expression_dependencies(expression, *, strict=True):
    """Return the node ids referenced as ``nodes['<literal>']`` in ``expression``.

    With ``strict=True`` (the v2 node contract) dynamic subscripts such as
    ``nodes[key]`` and bare ``nodes`` references (``nodes.get(...)``,
    ``'a' in nodes`` ...) raise ValueError because their dependencies cannot be
    determined statically. With ``strict=False`` they are ignored, which suits
    the final output/memory expressions that may see every node.
    """
    if not isinstance(expression, str):
        raise ValueError('expression must be a string')
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError as exc:
        raise ValueError(f'invalid expression syntax: {exc.msg}') from None
    deps = set()
    consumed = set()  # Name('nodes') objects that belong to a literal subscript
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == NODES_NAME:
            key = _literal_key(node.slice)
            if key is None:
                if strict:
                    raise ValueError("dynamic nodes[...] access is not allowed; use a literal id such as nodes['step']")
                continue
            deps.add(key)
            consumed.add(id(node.value))
    if strict:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == NODES_NAME and id(node) not in consumed:
                raise ValueError("bare 'nodes' reference is not allowed; use nodes['<id>'] with a literal id")
    return deps


def _node_expressions(node):
    """Expressions whose ``nodes[...]`` references count as dependencies of ``node``."""
    kind = node.get('kind')
    if kind == 'expression':
        return [node.get('expression')]
    if kind == 'jev':
        return [node.get('state', 'obs')] + ([node['questions_expression']] if 'questions_expression' in node else [])
    if kind == 'python':
        return []
    return [value for value in (node.get('expression'), node.get('state')) if value is not None]


def _node_ids(nodes):
    ids = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError(f'node {index} must be a dictionary')
        node_id = node.get('id')
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f'node {index} must have a non-empty string id')
        if node_id in ids:
            raise ValueError(f"duplicate node id '{node_id}'")
        ids.append(node_id)
    return ids


def compile_flow(spec):
    """Compile ``spec`` into ``{node_id: [dependency ids]}`` in spec node order.

    v1: prior-node chain (serial semantics). v2: explicit ``depends_on`` union
    statically inferred references; malformed lists, unknown ids, duplicates,
    self references, dynamic/bare ``nodes`` uses and cycles are rejected. Each
    dependency list is ordered by the dependency's position in the spec, so the
    result is deterministic regardless of ``depends_on`` order.
    """
    if not isinstance(spec, dict):
        raise ValueError('spec must be a dictionary')
    nodes = spec.get('nodes')
    if not isinstance(nodes, list):
        raise ValueError('spec nodes must be a list')
    version = spec.get('version', 1)
    if isinstance(version, bool) or version not in (1, 2, 3):
        raise ValueError(f'unsupported pipeline version {version!r}')
    ids = _node_ids(nodes)
    if version == 1:
        for node in nodes:
            if 'depends_on' in node:
                raise ValueError(f"node '{node['id']}' uses depends_on, which requires pipeline version 2")
        return {node_id: ([ids[index - 1]] if index else []) for index, node_id in enumerate(ids)}
    position = {node_id: index for index, node_id in enumerate(ids)}
    graph = {}
    for node in nodes:
        node_id = node['id']
        if node.get('kind') == 'python' and (version != 3 or 'depends_on' not in node):
            raise ValueError('Python nodes require version 3 and explicit depends_on')
        explicit = node.get('depends_on', [])
        if not isinstance(explicit, list) or not all(isinstance(dep, str) for dep in explicit):
            raise ValueError(f"node '{node_id}' depends_on must be a list of node id strings")
        if len(set(explicit)) != len(explicit):
            raise ValueError(f"node '{node_id}' depends_on contains duplicate ids")
        deps = set(explicit)
        for expression in _node_expressions(node):
            try:
                deps |= expression_dependencies(expression)
            except ValueError as exc:
                raise ValueError(f"node '{node_id}': {exc}") from None
        for dep in sorted(deps):
            if dep == node_id:
                raise ValueError(f"node '{node_id}' cannot depend on itself")
            if dep not in position:
                raise ValueError(f"node '{node_id}' depends on unknown node '{dep}'")
        graph[node_id] = sorted(deps, key=position.__getitem__)
    topological_order(graph)
    return graph


def topological_order(graph):
    """Return a deterministic topological order of ``graph``; raise ValueError on cycles."""
    remaining = {node_id: set(deps) for node_id, deps in graph.items()}
    order = []
    ready = [node_id for node_id in graph if not remaining[node_id]]
    while ready:
        node_id = ready.pop(0)
        order.append(node_id)
        for child, deps in remaining.items():
            if node_id in deps:
                deps.discard(node_id)
                if not deps:
                    ready.append(child)
    if len(order) != len(graph):
        cycle = [node_id for node_id in graph if node_id not in order]
        raise ValueError(f'dependency cycle detected among nodes: {cycle}')
    return order


def descendants(graph, roots):
    """Return the set of node ids that transitively depend on any id in ``roots``."""
    children = {}
    for node_id, deps in graph.items():
        for dep in deps:
            children.setdefault(dep, []).append(node_id)
    seen = set()
    stack = list(roots)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen
