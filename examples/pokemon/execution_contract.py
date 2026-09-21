"""Expose the actual fixed authoring rules to the reflection model."""
import inspect

from auto_jev import python_nodes, spec


def authoring_contract():
    return {
        'purpose': 'These are the existing execution rules, not additional strategy constraints. '
                   'Check every generated node against them before returning the complete pipeline.',
        'python': {
            'entrypoint': 'Define exactly one run(obs, nodes, memory), without defaults or annotations. '
                          'At module scope only function definitions and docstrings are allowed; '
                          'put lookup tables and other values inside functions.',
            'names': 'No Python identifier may start with an underscore, including local variables, '
                     'function names and arguments. Even the conventional throwaway variable _ is '
                     'forbidden; use ignored or dummy instead. String dictionary keys may contain underscores. '
                     'Do not use the forbidden identifiers below even as local variable names.',
            'forbidden_identifiers': sorted(python_nodes._FORBIDDEN),
            'builtins': sorted(python_nodes._BUILTINS),
            'prebound_modules': {
                'math': sorted(python_nodes._MATH),
                'statistics': sorted(python_nodes._STATISTICS),
            },
            'allowed_object_methods': sorted(python_nodes._METHODS),
            'allowed_ast_nodes': sorted(node.__name__ for node in python_nodes._AST),
            'source_validator': inspect.getsource(python_nodes.validate_python_source),
            'limits': {
                'source_bytes_per_node': python_nodes.SOURCE_BYTES,
                'ast_nodes_per_node': python_nodes.AST_NODES,
                'input_bytes_per_node': python_nodes.INPUT_BYTES,
                'output_bytes_per_node': python_nodes.OUTPUT_BYTES,
                'wall_seconds_per_node': python_nodes.WALL_SECONDS,
                'cpu_seconds_per_node': python_nodes.CPU_SECONDS,
                'memory_bytes_supervision_threshold': python_nodes.MEMORY_BYTES,
            },
            'wire_limits': 'Input bytes include the JSON object containing source and context. '
                           'Output bytes include the JSON ok/value wrapper. Leave room for these wrappers.',
            'worker_recursion_limit_statement': next(line.strip() for line in
                python_nodes._BOOTSTRAP.splitlines() if line.startswith('sys.setrecursionlimit(')),
            'values': 'Inputs and returned values must be finite JSON. No files, network, process access, '
                      'imports, classes, decorators, annotations, private attributes or attribute writes. '
                      'Only declared depends_on outputs are present in nodes; dependency outputs and '
                      'observations are immutable snapshots. Memory commits after the entire flow succeeds.',
        },
        'expressions': {
            'scope': 'Expression nodes, Jev state/questions_expression, final output and memory_update '
                     'use this smaller language, not ordinary Python. Put loops, comprehensions and '
                     'complex transformations in Python nodes.',
            'allowed_functions': sorted(spec.FUNCS),
            'allowed_ast_nodes': sorted(node.__name__ for node in spec.TYPES),
            'custom_functions': {
                'last(sequence, default=0)': 'Last element, or default for an empty sequence.',
                'get(mapping, key, default=None)': 'mapping.get(key, default).',
                'column(sequence, key)': 'List of item[key] for each item; missing keys fail.',
                'std(sequence)': 'Population standard deviation; 0 for fewer than two values.',
                'clip(value, low, high)': 'min(high, max(low, value)).',
                'mean(sequence)': 'statistics.mean(sequence); an empty sequence fails.',
            },
            'source_validator': inspect.getsource(spec._parse),
            'source_evaluator': inspect.getsource(spec.evaluate_expression),
        },
        'json_values': {
            'source_validator': inspect.getsource(spec._json),
            'limits': {name: getattr(spec, name) for name in
                       ('MAX_ITEMS', 'MAX_TEXT', 'MAX_JSON_VISITS', 'MAX_JSON_TEXT')},
        },
    }
