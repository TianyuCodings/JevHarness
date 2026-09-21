"""Pipeline executor.

Version 1 specs run node by node on the calling thread (serial semantics; every
node sees all earlier outputs). Version 2 specs run on a thread pool driven by
a dependency ready-queue: a node is submitted as soon as its own dependencies
have completed (no level barrier) and it only sees the outputs it declared or
statically references. Results and traces are always reported in spec node
order regardless of completion order. No LLM or provider objects are created
here; the ``jev`` client is injected.
"""
import copy
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .flow import compile_flow, descendants
from .spec import _json, evaluate_expression, validate_spec, validate_questions

__all__ = ['PipelineExecutionError', 'PipelineRuntime']


class PipelineExecutionError(ValueError):
    """A node or a finalization step failed.

    ``partial_result`` has the shape of a successful run: every node record
    produced (ok / error / blocked / cancelled), the outputs of succeeded nodes
    and the *unchanged* memory. ``cause`` is the underlying exception so
    provider/system errors stay recoverable for callers.
    """

    def __init__(self, message, *, partial_result=None, cause=None, causes=None):
        super().__init__(message)
        self.partial_result = partial_result if partial_result is not None else {}
        self.cause = cause
        self.causes = tuple(causes) if causes is not None else ((cause,) if cause is not None else ())
        self.__cause__ = cause


def _describe(exc):
    return f'{type(exc).__name__}: {exc}'


def _ms(now, origin):
    return (now - origin) * 1000.0


class PipelineRuntime:
    """Execute a validated pipeline spec against an injected Jev client."""

    def __init__(self, spec, jev, *, max_workers=8):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError('max_workers must be a positive integer')
        self.spec = validate_spec(spec)
        self.jev = jev
        self.max_workers = max_workers
        self.dependencies = compile_flow(self.spec)
        self.version = self.spec.get('version', 1)
        self.mode = 'parallel' if self.version >= 2 and max_workers > 1 else 'sequential'
        self._ids = list(self.dependencies)
        self._nodes = {node['id']: node for node in self.spec['nodes']}
        self._position = {node_id: index for index, node_id in enumerate(self._ids)}

    @property
    def execution(self):
        return {'mode': self.mode, 'max_workers': self.max_workers if self.version >= 2 else 1,
                'dependencies': copy.deepcopy(self.dependencies)}

    def run(self, obs, memory=None):
        run_start = time.perf_counter()
        if memory is not None and not isinstance(memory, dict):
            raise ValueError('memory must be a dictionary or None')
        _json(obs)
        _json(memory)
        obs = copy.deepcopy(obs)
        memory_before = copy.deepcopy(memory) if memory else {}  # the single memory snapshot for this run
        outputs, records, errors = {}, {}, {}
        if self.version >= 2:
            self._run_parallel(run_start, obs, memory_before, outputs, records, errors)
        else:
            self._run_sequential(run_start, obs, memory_before, outputs, records, errors)
        if errors:
            self._mark_unstarted(records, errors)
            trace = [records[node_id] for node_id in self._ids]
            first = min(errors, key=lambda node_id: (records[node_id]['ended_ms'], self._position[node_id]))
            finalization = {step: self._skipped_step(step, f"node '{first}' failed") for step in ('output', 'memory_update')}
            partial = self._result(None, memory_before, memory_before, outputs, trace, run_start, finalization)
            raise PipelineExecutionError(f"pipeline node '{first}' failed: {_describe(errors[first])}",
                                         partial_result=partial, cause=errors[first], causes=tuple(errors.values()))
        trace = [records[node_id] for node_id in self._ids]
        context = {'obs': obs, 'nodes': {node_id: outputs[node_id] for node_id in self._ids}, 'memory': memory_before}
        finalization = {}
        finalization['output'], output, error = self._finalize('output', self.spec.get('output'), context, run_start)
        if error is None:
            finalization['memory_update'], updated, error = self._finalize(
                'memory_update', self.spec.get('memory_update'), context, run_start)
            failed = 'memory_update'
        else:
            finalization['memory_update'] = self._skipped_step('memory_update', 'output evaluation failed')
            failed, updated = 'output', None
        if error is not None:
            partial = self._result(output, memory_before, memory_before, outputs, trace, run_start, finalization)
            raise PipelineExecutionError(f'pipeline finalization {failed!r} failed: {_describe(error)}',
                                         partial_result=partial, cause=error)
        return self._result(output, updated, memory_before, outputs, trace, run_start, finalization)

    # -- scheduling -----------------------------------------------------------
    def _run_sequential(self, run_start, obs, memory, outputs, records, errors):
        """v1: one node at a time on the calling thread; each node sees every earlier output."""
        for index, node_id in enumerate(self._ids):
            visible = {dep: outputs[dep] for dep in self._ids[:index]}
            record, result, error = self._execute(self._nodes[node_id], visible, obs, memory, run_start)
            records[node_id] = record
            if error is not None:
                errors[node_id] = error
                return
            outputs[node_id] = result

    def _run_parallel(self, run_start, obs, memory, outputs, records, errors):
        """v2: dependency-driven ready queue on a thread pool (no level barrier)."""
        if not self._ids:
            return
        remaining = {node_id: set(deps) for node_id, deps in self.dependencies.items()}
        children = {node_id: [] for node_id in self._ids}
        for node_id, deps in self.dependencies.items():
            for dep in deps:
                children[dep].append(node_id)
        ready = [node_id for node_id in self._ids if not remaining[node_id]]
        running = {}
        halted = False
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self._ids))) as pool:
            while ready or running:
                while ready and not halted and len(running) < self.max_workers:
                    node_id = ready.pop(0)
                    visible = {dep: outputs[dep] for dep in self.dependencies[node_id]}
                    future = pool.submit(self._execute, self._nodes[node_id], visible, obs, memory, run_start)
                    running[future] = node_id
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                unlocked = []
                for future in done:
                    node_id = running.pop(future)
                    record, result, error = future.result()
                    records[node_id] = record
                    if error is not None:
                        errors[node_id] = error
                        halted = True
                        continue
                    outputs[node_id] = result
                    for child in children[node_id]:
                        remaining[child].discard(node_id)
                        if not remaining[child]:
                            unlocked.append(child)
                if halted:  # stop launching, drop queued-but-unstarted work, drain what is running
                    ready.clear()
                    for future in list(running):
                        if future.cancel():
                            running.pop(future)
                else:
                    ready.extend(sorted(unlocked, key=self._position.__getitem__))

    # -- node execution -------------------------------------------------------
    def _base_record(self, node):
        record = {'id': node['id'], 'kind': node['kind'], 'depends_on': list(self.dependencies[node['id']]),
                  'status': 'ok', 'started_ms': None, 'ended_ms': None, 'elapsed_ms': None,
                  'input_context': None, 'output': None}
        if node['kind'] == 'expression':
            record['expression'] = node.get('expression')
        elif node['kind'] == 'python':
            record['source'] = node['source']
        else:
            record['state_expression'] = node.get('state', 'obs')
            record['questions'] = copy.deepcopy(node.get('questions'))
            record['state'] = None
            record['response'] = None
            if 'questions_expression' in node:
                record['questions_expression'] = node['questions_expression']
        return record

    def _execute(self, node, visible, obs, memory, run_start):
        """Run one node in isolation; ordinary exceptions are captured and returned as (record, result, error)."""
        record = self._base_record(node)
        started = time.perf_counter()
        record['started_ms'] = _ms(started, run_start)
        result, error = None, None
        try:
            context = {'obs': copy.deepcopy(obs), 'nodes': copy.deepcopy(visible), 'memory': copy.deepcopy(memory)}
            record['input_context'] = copy.deepcopy(context)
            if node['kind'] == 'expression':
                result = evaluate_expression(node['expression'], context)
            elif node['kind'] == 'python':
                from .python_nodes import evaluate_python
                result = evaluate_python(node['source'], context)
            else:
                state = evaluate_expression(record['state_expression'], context)
                record['state'] = copy.deepcopy(state)
                if 'questions_expression' in node:
                    record['questions'] = copy.deepcopy(evaluate_expression(node['questions_expression'], context))
                questions = record['questions']
                if self.version == 3:
                    validate_questions(questions, strict=True)
                response = copy.deepcopy(self.jev.judge(state, copy.deepcopy(questions), model=self.spec.get('jev_model')))
                record['response'] = response
                result = response['answers']
            _json(result)
            record['output'] = copy.deepcopy(result)
        except Exception as exc:  # BaseException (KeyboardInterrupt, SystemExit) propagates untouched
            result, error = None, exc
            record['status'] = 'error'
            record['error'] = _describe(exc)
            record['output'] = None
        ended = time.perf_counter()
        record['ended_ms'] = _ms(ended, run_start)
        record['elapsed_ms'] = _ms(ended, started)
        return record, result, error

    def _mark_unstarted(self, records, errors):
        """Record nodes that never ran: blocked (downstream of a failure) or cancelled."""
        failed = [node_id for node_id in self._ids if node_id in errors]
        downstream = {node_id: descendants(self.dependencies, [node_id]) for node_id in failed}
        for node_id in self._ids:
            if node_id in records:
                continue
            record = self._base_record(self._nodes[node_id])
            record['elapsed_ms'] = 0.0
            upstream = [failed_id for failed_id in failed if node_id in downstream[failed_id]]
            if upstream:
                record['status'] = 'blocked'
                record['error'] = f"blocked: upstream node(s) failed: {', '.join(upstream)}"
            else:
                record['status'] = 'cancelled'
                record['error'] = f"cancelled: run stopped after node(s) failed: {', '.join(failed)}"
            records[node_id] = record

    # -- finalization ---------------------------------------------------------
    def _finalize(self, step, expression, context, run_start):
        key = 'output' if step == 'output' else 'updated_memory'
        started = time.perf_counter()
        record = {'step': step, 'expression': expression, 'status': 'ok', 'started_ms': _ms(started, run_start),
                  'ended_ms': None, 'elapsed_ms': None, 'input_context': copy.deepcopy(context), key: None}
        value, error = None, None
        try:
            if step == 'memory_update' and expression is None:
                value = copy.deepcopy(context['memory'])  # no memory_update expression: memory unchanged
            else:
                value = evaluate_expression(expression, copy.deepcopy(context))
                if step == 'memory_update' and not isinstance(value, dict):
                    raise ValueError('memory_update must return a dictionary')
            record[key] = copy.deepcopy(value)
        except Exception as exc:
            value, error = None, exc
            record['status'] = 'error'
            record['error'] = _describe(exc)
        ended = time.perf_counter()
        record['ended_ms'] = _ms(ended, run_start)
        record['elapsed_ms'] = _ms(ended, started)
        return record, value, error

    def _skipped_step(self, step, reason):
        expression = self.spec.get('output') if step == 'output' else self.spec.get('memory_update')
        key = 'output' if step == 'output' else 'updated_memory'
        return {'step': step, 'expression': expression, 'status': 'cancelled', 'error': f'cancelled: {reason}',
                'started_ms': None, 'ended_ms': None, 'elapsed_ms': 0.0, 'input_context': None, key: None}

    def _result(self, output, memory, memory_before, outputs, trace, run_start, finalization):
        return copy.deepcopy({
            'output': output,
            'memory': memory,
            'memory_before': memory_before,
            'nodes': {node_id: outputs[node_id] for node_id in self._ids if node_id in outputs},
            'trace': trace,
            'elapsed_ms': _ms(time.perf_counter(), run_start),
            'execution': self.execution,
            'finalization': finalization,
        })
