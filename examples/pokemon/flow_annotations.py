"""Source-reviewed UI annotations for the archived mixed Pokemon search.

This is presentation metadata, never an input to policy execution. Semantic
claims apply only to an exact child/parent specification pair reviewed here.
Unknown or modified specifications expose structural dependencies only. No
strategy is executed and no archive, evaluation or private partition is read.

Damage, speed and coverage labels describe the candidate's approximations,
not an exact simulator or knowledge of the opponent's hidden configuration.
"""
from __future__ import annotations

import ast
import hashlib
import json


SEED = '3119e57ae37a8834de33eab7f38eb3b97635372fdedf7b9496e0d311501494ab'
R1 = '8751159544960a76d06a0bcbd7cf26d4b781b9e30706fdc030c50e94b73822d8'
R2 = '809c0e2c13015914e193adca8076ea44e11838c27b847acc4be9ee1e721d4905'
R3 = '0617b0bb9505cddfc319231e6b2226924e1b1d40280b09486ee16d838342a502'
R4 = 'a802df0d514c3137e33027f819874aa3f288e09a0b7ed499ae4d0514d33efadf'
R5 = 'c699bd0f91280111cade3d3bdd11c9672347594d71b2af34aa437682cf5b8228'
REVIEWED_HASHES = (SEED, R1, R2, R3, R4, R5)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return _sha(_canonical(value))


def _c(label, status, function, text='', parent=None):
    """A source feature with optional (function, exact excerpt) parent evidence."""
    return (label, status, ('code', function, text),
            ('code', *parent) if parent is not None else None)


def _v(label, status, field, parent=None):
    return (label, status, ('field', field), ('field', parent) if parent else None)


def _q(label, status, question, parent=None):
    return _v(label, status, 'questions.' + question + '.instructions',
              'questions.' + parent + '.instructions' if parent else None)


def _n(title, description, parent_node, *features):
    return {'title': title, 'description': description, 'parent_node': parent_node,
            'features': features}


_REVIEWS = {
    SEED: {
        'parent': None,
        'summary': 'A rough damage index feeds two Jev branches; a small danger rule checks the chosen action.',
        'changes': ['Baseline: damage index, per-action criteria, tactical questions and a final legal action.'],
        'nodes': {
            'features': _n('Describe legal actions',
                'Attach a rough damage index to each action and build the action-choice criteria.', None,
                _c('Power, accuracy + STAB index', 'retained', 'run', 'damage_index ='),
                _c('All legal action candidates', 'retained', 'run', 'legal_actions'),
                _c('Dynamic action criteria', 'retained', 'run', 'criteria')),
            'tactics': _n('Assess the position',
                'Ask about danger and a passive turn. The final decision consumes danger only.', None,
                _q('Danger judgment', 'retained', 'danger'),
                _q('Setup question (not consumed)', 'retained', 'setup'),
                _v('Visible position + full history', 'retained', 'state')),
            'action_choice': _n('Choose an action',
                'Jev chooses one legal action from the dynamically generated criteria.', None,
                _v('Dynamic choice interface', 'retained', 'questions_expression'),
                _v('Own + public opponent teams', 'retained', 'state'),
                _v('Full battle history', 'retained', 'state')),
            'decision': _n('Apply the danger rule',
                'Use the Jev choice, replacing a low-danger switch with the highest damage-index attack.', None,
                _c('Consume Jev action choice', 'retained', 'run', 'selected ='),
                _c('Low-danger switch override', 'retained', 'run', 'danger < 0.15'),
                _c('Highest-index attack', 'retained', 'run', 'max(attacks')),
        },
    },
    R1: {
        'parent': SEED,
        'summary': 'Replace the rough index with matchup estimates, speed and switch-entry costs, then combine code scores with Jev.',
        'changes': [
            'Damage scoring now estimates defenses and remaining HP; speed, priority and switch-entry damage guide action values.',
            'Existing dynamic criteria become scored shortlists with damage and risk explanations.',
            'Separate switch, recovery and setup judgments feed weighted scoring, with a finisher override and a 15-point choice gate.',
        ],
        'nodes': {
            'features': _n('Calculate matchup values',
                'Estimate damage and survival from public species data, actual own stats and revealed moves.', 'features',
                _c('Defense + HP damage estimates', 'changed', 'movecand', "frac =", parent=('run', 'damage_index =')),
                _c('Speed + priority estimates', 'added', 'movecand', 'wefirst ='),
                _c('Switch-entry risk + repeat cost', 'added', 'switchcand', "ctx['streak']"),
                _c('Scored shortlist criteria', 'changed', 'build', 'criteria', parent=('run', 'criteria')),
                _c('Recovery + setup values', 'added', 'movecand', 'elif isheal'),
                _c('Finisher detection', 'added', 'build', 'finisher')),
            'tactics': _n('Assess tactical options',
                'Judge switching, recovery and setup separately using the calculated summary and option descriptions.', 'tactics',
                _q('Switch-or-attack judgment', 'changed', 'switch_now', 'danger'),
                _q('Separate recovery judgment', 'changed', 'recover_now', 'setup'),
                _q('Separate setup judgment', 'changed', 'setup_now', 'setup'),
                _v('Calculated options + recent log', 'changed', 'state', 'state')),
            'action_choice': _n('Choose from scored options',
                'Keep the dynamic choice interface while supplying a calculated summary and the latest 30 log lines.', 'action_choice',
                _v('Dynamic choice interface', 'retained', 'questions_expression', 'questions_expression'),
                _v('Calculated position summary', 'changed', 'state', 'state'),
                _v('30-line recent log', 'changed', 'state', 'state')),
            'decision': _n('Combine code scores + Jev',
                'Apply tactical score adjustments and accept the Jev choice when it is within 15 points of the best score.', 'decision',
                _c('Weighted tactical judgments', 'changed', 'decide', 'scored', parent=('run', 'danger')),
                _c('15-point choice gate', 'added', 'decide', '15.0'),
                _c('Finisher override', 'added', 'decide', 'finisher'),
                _c('Unsafe-switch filtering', 'added', 'decide', 'unsafe')),
        },
    },
    R2: {
        'parent': R1,
        'summary': 'Add a public-log scout before the calculator, then use immunity evidence and hypothetical coverage risk to revise actions.',
        'changes': [
            'A new scout node extracts observed failures, damage fractions and revealed abilities/items from the public log.',
            'The calculator uses scout evidence, filters ineffective actions and estimates hypothetical super-effective coverage.',
            'A new hidden-KO question changes attack/switch scores; the existing 15-point choice gate remains.',
        ],
        'nodes': {
            'scout': _n('Read public battle evidence',
                'Parse public messages before calculation. Revealed items are recorded; downstream scoring uses failures, damage and abilities.', None,
                _c('Observed immunity / failure evidence', 'added', 'scan', 'blocked'),
                _c('Observed damage fractions', 'added', 'scan', 'damage'),
                _c('Revealed ability / item log', 'added', 'scan', 'abilities')),
            'features': _n('Recalculate with scout evidence',
                'Use public-log evidence and hypothetical coverage to penalize ineffective moves and risky switches.', 'features',
                _c('Observed immunity + ability checks', 'changed', 'strike', 'blocked', parent=('movecand', 'mult =')),
                _c('Hypothetical hidden coverage', 'added', 'threatpack', 'risk'),
                _c('Ineffective-action filtering', 'added', 'movecand', 'useless ='),
                _c('Switch out of no-damage matchups', 'changed', 'switchcand', "ctx['stuck']", parent=('switchcand', "ctx['streak']")),
                _v('Scout dependency', 'added', 'depends_on', 'depends_on')),
            'tactics': _n('Assess hidden KO risk',
                'Add an unrevealed-threat judgment while preserving separate recovery and setup questions.', 'tactics',
                _q('Hidden KO risk question', 'added', 'hidden_ko_risk'),
                _q('No-damage switch judgment', 'changed', 'switch_now', 'switch_now'),
                _q('Recovery judgment', 'retained', 'recover_now', 'recover_now'),
                _q('Setup judgment', 'retained', 'setup_now', 'setup_now')),
            'action_choice': _n('Choose with revised context',
                'Use the same dynamic choice interface, with scout-informed calculations and a 26-line recent log.', 'action_choice',
                _v('Dynamic choice interface', 'retained', 'questions_expression', 'questions_expression'),
                _v('Own + public opponent teams', 'retained', 'state', 'state'),
                _v('26-line recent log', 'changed', 'state', 'state')),
            'decision': _n('Weight hidden-threat risk',
                'Adjust attack and switch scores using the hidden-KO answer after filtering ineffective actions.', 'decision',
                _c('Hidden-KO score adjustment', 'added', 'decide', 'korisk'),
                _c('Ineffective-action filtering', 'added', 'decide', 'useless'),
                _c('15-point choice gate', 'retained', 'decide', '15.0', parent=('decide', '15.0')),
                _c('Finisher override', 'retained', 'decide', 'finisher', parent=('decide', 'finisher'))),
        },
    },
    R3: {
        'parent': R1,
        'summary': 'Extend R1 with turns-to-KO races and hypothetical hidden coverage; refine its damage, speed, switch risk and action criteria.',
        'changes': [
            'From R1: add turns-to-KO races, hypothetical hidden coverage and remaining-team matchup value.',
            'Refine the existing damage/speed model with revealed abilities, status and sand; enrich existing action criteria with race and switch-risk explanations.',
            'Rename tactics/action_choice to plan/pick, add a hidden-coverage judgment and tighten the choice gate from 15 to 10 points.',
        ],
        'nodes': {
            'features': _n('Calculate damage races + risk',
                'Extend R1\'s approximate calculator with KO races, hypothetical coverage and remaining-team matchups.', 'features',
                _c('Turns-to-KO races', 'added', 'matchup', 'turnsus'),
                _c('Hypothetical hidden coverage', 'added', 'threaton', "ctx['unknown']"),
                _c('Coverage-aware switch-entry risk', 'changed', 'switchcand', 'entry', parent=('switchcand', 'inf')),
                _c('Ability + status damage adjustments', 'changed', 'oppctxof', "status == 'brn'", parent=('oppstats', 'def oppstats')),
                _c('Status-adjusted speed', 'changed', 'ourprofile', "status == 'par'", parent=('build', 'spe')),
                _c('Remaining-team switch value', 'added', 'switchcand', 'teamm'),
                _c('Race-aware action criteria', 'changed', 'instructions', 'how many turns', parent=('instructions', 'predicted'))),
            'plan': _n('Assess tactical + coverage risk',
                'Replace tactics in the same branch; add hidden coverage and use damage races in switch/recovery judgments.', 'tactics',
                _q('Hidden coverage question', 'added', 'hidden_coverage'),
                _q('Race-aware switch judgment', 'changed', 'switch_now', 'switch_now'),
                _q('Race-aware recovery judgment', 'changed', 'recover_now', 'recover_now'),
                _q('Setup judgment', 'retained', 'setup_now', 'setup_now')),
            'pick': _n('Choose from race-aware options',
                'Replace action_choice while keeping dynamic criteria; read computed team matchups and the latest 24 log lines.', 'action_choice',
                _v('Dynamic choice interface', 'retained', 'questions_expression', 'questions_expression'),
                _v('Calculated team matchup context', 'changed', 'state', 'state'),
                _v('24-line recent log', 'changed', 'state', 'state')),
            'decision': _n('Balance scores + hidden risk',
                'Add coverage-sensitive penalties to R1\'s weighted scoring and tighten the Jev acceptance margin.', 'decision',
                _c('Hidden-risk score weighting', 'added', 'decide', 'hiddenpen'),
                _c('10-point choice gate', 'changed', 'decide', '10.0', parent=('decide', '15.0')),
                _c('Switch / recovery weights', 'changed', 'decide', '30.0', parent=('decide', '26.0')),
                _c('Finisher override', 'retained', 'decide', 'finisher', parent=('decide', 'finisher'))),
        },
    },
    R4: {
        'parent': R3,
        'summary': 'Extend R3 with explicit ability assumptions and public failure evidence to avoid wasted turns and escape no-damage matchups.',
        'changes': [
            'Add species-based ability assumptions and public failure-log checks; assumptions remain distinct from revealed abilities.',
            'Filter wasted attacks/recovery/status options and encourage switching when the active Pokemon cannot deal damage.',
            'Add a blocked-attack Jev question and penalty; preserve hidden-coverage scoring and widen the choice gate from 10 to 12 points.',
        ],
        'nodes': {
            'features': _n('Detect ineffective turns',
                'Extend the race calculator with ability assumptions, public failures, wasted-action checks and no-damage escape costs.', 'features',
                _c('Species-based ability assumptions', 'added', 'priorof', 'priortable'),
                _c('Public failure-log evidence', 'added', 'scanlog', 'fails'),
                _c('Wasted-action checks', 'added', 'movecand', 'wasted ='),
                _c('No-damage switch escape', 'changed', 'switchcand', 'if nodamage', parent=('switchcand', '24.0')),
                _c('Sand chip in damage races', 'changed', 'threaton', "prof['chip']", parent=('threaton', 'risk =')),
                _c('Filtered action criteria', 'changed', 'build', "cnd['wasted']", parent=('build', 'criteria'))),
            'plan': _n('Assess blocked-attack risk',
                'Add a blocked-attack question to the existing tactical and hidden-coverage judgments.', 'plan',
                _q('Blocked-attack question', 'added', 'blocked_attack'),
                _q('Failed-recovery guard', 'changed', 'recover_now', 'recover_now'),
                _q('No-damage switch judgment', 'changed', 'switch_now', 'switch_now'),
                _q('Hidden coverage question', 'retained', 'hidden_coverage', 'hidden_coverage')),
            'pick': _n('Choose with wasted options marked',
                'Retain dynamic choice and team context, adding the calculator\'s explicit list of ineffective options.', 'pick',
                _v('Ineffective-option context', 'added', 'state'),
                _v('Dynamic choice interface', 'retained', 'questions_expression', 'questions_expression'),
                _v('Team context + 24-line log', 'retained', 'state', 'state')),
            'decision': _n('Penalize blocked attacks',
                'Filter wasted actions, penalize a likely blocked best attack and use a 12-point choice gate.', 'decision',
                _c('Wasted-action pool filtering', 'added', 'poolof', 'wasted'),
                _c('Blocked-attack score penalty', 'added', 'decide', 'blk > 0.55'),
                _c('12-point choice gate', 'changed', 'decide', '12.0', parent=('decide', '10.0')),
                _c('Hidden-risk score weighting', 'retained', 'decide', 'hiddenpen', parent=('decide', 'hiddenpen'))),
        },
    },
    R5: {
        'parent': R1,
        'summary': 'Branch from R1 with public immunity evidence, weather and ability-risk estimates, plus a Jev judgment about accepting a sacrifice.',
        'changes': [
            'From R1: replace features with analysis, adding public immunity logs, species-based ability risk and weather/chip adjustments.',
            'Add hypothetical hidden-coverage estimates, blocked-action filtering and a remaining-team matchup outlook.',
            'Merge recovery/setup into a passive-turn question, add sacrifice judgment and use a 12-point choice gate.',
        ],
        'nodes': {
            'analysis': _n('Analyze immunity + matchup risk',
                'Replace R1\'s features node with a calculator using public immunity evidence, ability assumptions and weather.', 'features',
                _c('Public immunity / ability log', 'added', 'parselog', "'-immune'"),
                _c('Species-based ability risk', 'added', 'oppabil', "ctx['risks']"),
                _c('Weather-aware damage estimates', 'changed', 'incoming', 'chip', parent=('threatdmg', 'def threatdmg')),
                _c('Hypothetical hidden coverage', 'added', 'incoming', 'cover ='),
                _c('Blocked-action shortlist', 'changed', 'build', "c['blocked']", parent=('build', 'criteria')),
                _c('Remaining-team matchup outlook', 'added', 'outlook', 'best remaining matchup')),
            'tactics': _n('Assess switches + sacrifices',
                'Keep the switch judgment, merge passive-move questions and ask whether accepting a faint is worthwhile.', 'tactics',
                _q('Sacrifice / trade judgment', 'added', 'sack_ok'),
                _q('Combined passive-turn judgment', 'changed', 'passive_turn', 'recover_now'),
                _q('Switch-or-move judgment', 'changed', 'switch_now', 'switch_now'),
                _v('26-line recent log', 'changed', 'state', 'state')),
            'action_choice': _n('Choose from filtered options',
                'Read the replacement analysis node through the existing dynamic choice interface.', 'action_choice',
                _v('Analysis criteria dependency', 'changed', 'questions_expression', 'questions_expression'),
                _v('Own + public opponent teams', 'retained', 'state', 'state'),
                _v('26-line recent log', 'changed', 'state', 'state')),
            'decision': _n('Balance action value + sacrifice',
                'Adjust fainting cost with the sacrifice answer and combine switch/passive judgments with calculated values.', 'decision',
                _c('Sacrifice-adjusted fainting cost', 'added', 'decide', '(sack - 0.5)'),
                _c('Combined passive-turn weighting', 'changed', 'decide', 'passive', parent=('decide', 'recover_now')),
                _c('12-point choice gate', 'changed', 'decide', '12.0', parent=('decide', '15.0')),
                _c('Blocked-action guard', 'added', 'decide', "chosen.get('blocked')")),
        },
    },
}


def _anchor(node, selector):
    if selector[0] == 'field':
        value = node
        for part in selector[1].split('.'):
            value = value[part]
        return {'field': selector[1], 'text': value if isinstance(value, str) else _canonical(value),
                'value_sha256': _hash(value)}
    source = node['source']
    function = next(f for f in ast.parse(source).body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == selector[1])
    segment = ast.get_source_segment(source, function)
    needle = selector[2] or segment.splitlines()[0]
    offset = segment.index(needle)
    # Short exact source evidence, with the complete function/source hashes.
    line_start = segment.rfind('\n', 0, offset) + 1
    line_end = segment.find('\n', offset + len(needle))
    if line_end < 0:
        line_end = len(segment)
    excerpt = segment[line_start:line_end]
    if len(excerpt) > 350:
        excerpt = needle
    return {'function': function.name, 'function_sha256': _sha(segment),
            'line': function.lineno + segment.count('\n', 0, offset),
            'function_start_line': function.lineno, 'function_end_line': function.end_lineno,
            'text': excerpt}


def _node_evidence(node):
    result = {'node_id': node['id'], 'node_sha256': _hash(node)}
    if isinstance(node.get('source'), str):
        result['source_sha256'] = _sha(node['source'])
    return result


def _fallback(candidate_hash, spec, parent_hash, parent_spec, reason):
    old = {n['id']: n for n in (parent_spec or {}).get('nodes', [])}
    annotations = {}
    for node in spec.get('nodes', []):
        parent = old.get(node['id'])
        change = 'retained' if parent is None and parent_hash is None or parent == node else 'changed' if parent else 'added'
        deps = node.get('depends_on', [])
        status = 'retained' if not parent_hash or parent and parent.get('depends_on', []) == deps else 'changed' if parent else 'added'
        evidence = {'candidate_hash': candidate_hash, **_node_evidence(node),
                    'anchors': [_anchor(node, ('field', 'depends_on'))] if 'depends_on' in node else []}
        if parent:
            evidence.update(parent_candidate_hash=parent_hash, parent_node_id=parent['id'],
                            parent_node_sha256=_hash(parent))
        annotations[node['id']] = {
            'title': node['id'],
            'description': 'Unreviewed source. Only declared dependencies are compared with the parent.',
            'change_type': change, 'parent_node': parent['id'] if parent else None,
            'features': [{'label': 'Dependencies: ' + (', '.join(deps) if deps else 'none'),
                          'status': status, 'evidence': evidence}],
        }
    return {'review_status': 'unreviewed', 'review_reason': reason,
            'summary': 'Unreviewed source. Semantic feature changes have not been reviewed.',
            'changes': [], 'nodes': annotations}


def annotate_candidate(candidate_hash, spec, *, parent_hash=None, parent_spec=None):
    """Annotate a specification without trusting caller-supplied identity strings.

    Renamed nodes map to their reviewed semantic predecessor. ``added`` on a
    feature refers to this exact parent, not the preceding numbered round.
    Baseline features use ``retained`` with no parent; the UI labels that case
    BASELINE. All returned structures are newly constructed and can be edited
    by a consumer without changing this module's reviewed templates.
    """
    review = _REVIEWS.get(candidate_hash)
    reason = 'No semantic review for this candidate hash.'
    try:
        current_matches = _hash(spec) == candidate_hash
        parent_matches = ((parent_hash is None and parent_spec is None) or
                          (parent_spec is not None and _hash(parent_spec) == parent_hash))
    except (TypeError, ValueError):
        current_matches = parent_matches = False
    if review and not current_matches:
        reason = 'Candidate content does not match its reviewed hash.'
    elif review and (parent_hash != review['parent'] or not parent_matches):
        reason = 'Parent identity or content does not match the reviewed lineage.'
    elif review:
        nodes = {n['id']: n for n in spec['nodes']}
        old = {n['id']: n for n in (parent_spec or {}).get('nodes', [])}
        annotations = {}
        try:
            if set(nodes) != set(review['nodes']):
                raise ValueError('Reviewed node set differs')
            for ident, template in review['nodes'].items():
                node = nodes[ident]
                parent_id = template['parent_node']
                parent = old[parent_id] if parent_id else None
                features = []
                for label, status, selector, parent_selector in template['features']:
                    evidence = {'candidate_hash': candidate_hash, **_node_evidence(node),
                                'anchors': [_anchor(node, selector)]}
                    if parent is not None:
                        evidence.update(parent_candidate_hash=parent_hash, parent_node_id=parent_id,
                                        parent_node_sha256=_hash(parent), parent_anchors=[])
                        if 'source' in parent:
                            evidence['parent_source_sha256'] = _sha(parent['source'])
                        if parent_selector:
                            evidence['parent_anchors'].append(_anchor(parent, parent_selector))
                    features.append({'label': label, 'status': status, 'evidence': evidence})
                change = 'retained' if parent_hash is None or parent == node else 'changed' if parent is not None else 'added'
                annotations[ident] = {'title': template['title'], 'description': template['description'],
                                      'parent_node': parent_id, 'change_type': change, 'features': features}
        except (KeyError, ValueError, StopIteration, SyntaxError) as exc:
            # A stale annotation must never silently claim a semantic review.
            return _fallback(candidate_hash, spec, parent_hash, parent_spec,
                             'Reviewed source evidence could not be resolved: ' + type(exc).__name__)
        return {'review_status': 'reviewed', 'review_method': 'Exact specification and parent hashes; source review.',
                'summary': review['summary'], 'changes': list(review['changes']), 'nodes': annotations}
    return _fallback(candidate_hash, spec, parent_hash, parent_spec, reason)
