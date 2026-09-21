"""An evolvable mixed strategy and its matched code-only baseline."""
import copy


FEATURES = '''def run(obs, nodes, memory):
    active = obs.get("active") or {}
    target = obs.get("opponent_active") or {}
    candidates = []
    for action in obs["legal_actions"]:
        a = dict(action)
        f = a.get("move") or a.get("pokemon") or a
        power = f.get("base_power", 0) or 0
        accuracy = f.get("accuracy", 1)
        if accuracy is None:
            accuracy = 1
        if accuracy is True:
            accuracy = 1
        if accuracy > 1:
            accuracy = accuracy / 100
        multiplier = f.get("type_multiplier", 1)
        if multiplier is None:
            multiplier = 1
        move_type = str(f.get("type", "")).lower()
        own_types = [str(t).lower() for t in active.get("types", [])]
        stab = 1.5 if move_type in own_types else 1.0
        attack_stat = "atk" if str(f.get("category", "")).lower() == "physical" else "spa"
        stats = active.get("stats") or {}
        damage_index = power * accuracy * multiplier * stab * max(0.5, stats.get(attack_stat, 100) / 100)
        a["damage_index"] = damage_index
        candidates.append(a)
    criteria = {}
    for a in candidates:
        criteria[a["id"]] = str(a)
    return {
        "candidates": candidates,
        "active": active,
        "opponent": target,
        "field": obs.get("field", {}),
        "phase": obs.get("phase"),
        "choice_questions": {
            "action": {
                "type": "choice",
                "instructions": "Select the legal action most likely to improve eventual battle victory. Use the supplied candidate features, visible opponent information, and own remaining team. Damage index is only a rough heuristic: consider immunities, setup, recovery, status, priority and preserving useful team members. Never assume unknown opponent moves or items. Return one supplied action ID.",
                "criteria": criteria
            }
        }
    }
'''

COMBINE = '''def run(obs, nodes, memory):
    candidates = nodes["features"]["candidates"]
    selected = nodes["action_choice"]["action"]["choice"]
    danger = nodes["tactics"]["danger"]["noul"]
    chosen = next(a for a in candidates if a["id"] == selected)
    attacks = [a for a in candidates if a["kind"] == "move" and a["damage_index"] > 0]
    if danger < 0.15 and chosen["kind"] == "switch" and attacks:
        return max(attacks, key=lambda a: a["damage_index"])["id"]
    return selected
'''

BASELINE = '''def run(obs, nodes, memory):
    candidates = nodes["features"]["candidates"]
    moves = [a for a in candidates if a["kind"] == "move"]
    if moves:
        return max(moves, key=lambda a: a["damage_index"])["id"]
    return candidates[0]["id"]
'''


def seed_spec():
    return {
        'version': 3, 'name': 'Pokemon parallel tactical policy', 'jev_model': 'typesafe-ai/jev',
        'nodes': [
            {'id': 'features', 'kind': 'python', 'source': FEATURES, 'depends_on': []},
            {'id': 'tactics', 'kind': 'jev', 'depends_on': ['features'],
             'state': "{'position':nodes['features'], 'own_team':obs['own_team'], 'history':obs['history']}",
             'questions': {
                 'danger': {'type': 'noul', 'instructions': 'Given the visible matchup and revealed information, is staying with the active Pokemon likely to lead to it fainting before it can make a useful contribution? Unknown opponent moves and stats remain uncertain.'},
                 'setup': {'type': 'noul', 'instructions': 'Does the visible position offer a favorable opportunity to use a non-damaging setup, recovery, or status move instead of immediate attacking?'},
             }},
            {'id': 'action_choice', 'kind': 'jev', 'depends_on': ['features'],
             'state': "{'position':nodes['features'], 'own_team':obs['own_team'], 'opponent_public_team':obs['opponent_public_team'], 'history':obs['history']}",
             'questions_expression': "nodes['features']['choice_questions']"},
            {'id': 'decision', 'kind': 'python', 'source': COMBINE,
             'depends_on': ['features', 'tactics', 'action_choice']},
        ],
        'output': "nodes['decision']",
        'memory_update': "{'previous_action':nodes['decision'], 'previous_turn':obs['turn']}",
    }


def code_baseline_spec():
    spec = seed_spec()
    spec['name'] = 'Code-only damage heuristic'
    spec['nodes'] = [copy.deepcopy(spec['nodes'][0]),
                     {'id': 'decision', 'kind': 'python', 'source': BASELINE, 'depends_on': ['features']}]
    return spec


TASK_CONTEXT = {
    'objective': 'Maximize average engine-confirmed battle reward: win=1, loss=0, draw=0.5.',
    'description': 'Pokemon generation 9 custom 3v3 singles pilot with preassigned teams, fixed initial team order, and Terastallization disabled. Evolve battle decisions, not team construction. Each task instance fixes the opponent policy, teams, and simulator RNG seed. Runtime sees only player-visible information.',
    'observation': 'JSON player view: turn, phase, own_team, active, opponent_active, opponent_public_team, field, complete player-visible protocol history, and legal_actions. Candidate source code has no game-engine handle or raw hidden opponent team. Public move/species descriptions are game knowledge, not private state.',
    'output': 'Return exactly one string ID from obs.legal_actions for the current request. Do not return a command, move name, index, or selector. Unknown hidden information must remain unknown.',
    'feedback': 'All selected training battle trajectories are provided, including every observation, code and Jev node context, question, response, action, memory update, and outcome. Post-game episode configurations and results are hindsight training feedback, not permitted runtime observations.',
    'search_space': 'Version 3 code and Jev graph: add/delete/rewire nodes, write functional Python run(obs,nodes,memory), change dynamic questions, state transformations, criteria, output combination and memory. Rules, legal action execution and external reward evaluator are fixed.',
}
