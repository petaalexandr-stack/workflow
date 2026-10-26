"""Contract tests: every module can be independently executed and rejected."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workflow_engine.cli import load_idea
from workflow_engine.controller import ModuleController
from workflow_engine.provider import AgentRegistry, DemoProvider, ApprovalOnlyProvider, packaged_config_root
from workflow_engine.schema import WorkflowError, normalize_route, route_diagram
from workflow_engine.store import StateStore


class RecordingDemo(DemoProvider):
    def __init__(self):
        self.calls = []
        self.contexts = {}
        self.fail = set()
        self.incomplete_design = False

    def run_agent(self, agent, context, registry, *, project, allow_writes, allow_shell):
        self.calls.append(agent)
        self.contexts[(context['stage']['id'], agent)] = context
        out = super().run_agent(agent, context, registry, project=project,
                                allow_writes=allow_writes, allow_shell=allow_shell)
        if agent in self.fail:
            out['status'] = 'blocked'
        if agent == 'architect' and self.incomplete_design:
            out['system_design']['scope'] = 'todo'
        return out


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        (self.project / 'ideas').mkdir()
        self.store = StateStore(self.project/'.workflow'/'state.db')
        self.addCleanup(self.store.close)
        self.provider = RecordingDemo()
        self.ctl = ModuleController(self.store, AgentRegistry(packaged_config_root()), self.provider)
        self.run = self.ctl.new(self.project, 'Создать большую сложную систему авторизации для приложения')['id']

    def understanding(self):
        s = self.ctl.next(self.run)
        self.assertEqual(s['status'], 'await_understanding')
        return self.ctl.approve(self.run, s['gate']['token'])

    def route(self, modules=None, mode='none'):
        self.understanding()
        s = self.ctl.next(self.run)
        self.assertEqual(s['status'], 'await_route')
        if modules is not None:
            s = self.ctl.set_route(self.run, {'modules': modules, 'system_design_mode': mode,
                                               'rationale': 'test', 'risks': []})
        return self.ctl.approve(self.run, s['gate']['token'])

    def execute(self, note='explicit acceptance'):
        s = self.ctl.next(self.run)
        self.assertEqual(s['status'], 'await_module_review')
        return self.ctl.approve(self.run, s['gate']['token'], note)

    def test_understanding_gate_blocks_execution(self):
        s = self.ctl.next(self.run)
        with self.assertRaises(WorkflowError):
            self.ctl.next(self.run)
        self.assertEqual(self.provider.calls, [])
        self.assertFalse(s['understanding_approved'])
        self.assertFalse(s['route_approved'])

    def test_second_approval_is_independent(self):
        self.understanding()
        s = self.ctl.next(self.run)
        self.assertEqual(self.provider.calls, [])
        with self.assertRaises(WorkflowError):
            self.ctl.next(self.run)
        self.assertFalse(s['route_approved'])

    def test_stale_token_rejected(self):
        self.understanding()
        s = self.ctl.next(self.run)
        old = s['gate']['token']
        s = self.ctl.set_route(self.run, {'modules': ['evals', 'documentation'], 'system_design_mode': 'none'})
        self.assertNotEqual(old, s['gate']['token'])
        with self.assertRaises(WorkflowError):
            self.ctl.approve(self.run, old)

    def test_modules_are_ordered_and_not_directly_invoked(self):
        self.route(['evals', 'planning', 'documentation'])
        s = self.ctl.next(self.run)
        self.assertEqual(self.provider.calls, ['evals'])
        self.assertEqual(s['status'], 'await_module_review')
        with self.assertRaises(WorkflowError):
            self.ctl.next(self.run)
        s = self.ctl.approve(self.run, s['gate']['token'])
        self.assertEqual(s['status'], 'ready_module')
        self.execute()
        self.execute()
        self.assertEqual(self.provider.calls, ['evals', 'plan', 'documentation'])
        self.assertEqual(self.ctl.load(self.run)['status'], 'demo_complete')

    def test_doc_is_inserted_by_controller(self):
        route = normalize_route(['evals'], 'none')
        self.assertEqual([m['template'] for m in route], ['evals', 'documentation'])
        self.assertEqual(route[-1]['inputs'], ['evals'])

    def test_doc_cannot_appear_in_middle(self):
        with self.assertRaises(WorkflowError):
            normalize_route(['documentation', 'evals'])

    def test_module_inputs_must_be_previous_approved(self):
        with self.assertRaises(WorkflowError):
            normalize_route([{'id':'evals','template':'evals','inputs':['developer']}, 'documentation'])

    def test_duplicate_template_with_distinct_ids(self):
        modules = normalize_route([
            {'id':'backend','template':'development','inputs':[]},
            {'id':'frontend','template':'development','inputs':['backend']},
        ])
        self.assertEqual([m['id'] for m in modules], ['backend','frontend','documentation'])
        self.assertEqual([m['template'] for m in modules], ['development','development','documentation'])

    def test_targeted_role_revision_preserves_other_agent(self):
        self.route(['discovery'])
        self.ctl.next(self.run)
        self.assertEqual(self.provider.calls, ['researcher','ux-designer'])
        s = self.ctl.revise(self.run, 'ux-designer', 'Исправить только экран регистрации')
        self.assertEqual(s['results']['discovery']['pending_agents'], ['ux-designer'])
        self.ctl.next(self.run)
        self.assertEqual(self.provider.calls, ['researcher','ux-designer','ux-designer'])
        s = self.ctl.load(self.run)
        self.assertEqual(set(s['results']['discovery']['agent_results']), {'researcher','ux-designer'})
        self.assertIn('Исправить только экран регистрации',self.provider.contexts[('discovery','ux-designer')]['feedback_to_this_agent'])

    def test_revise_invalidates_old_token(self):
        self.route(['discovery'])
        s = self.ctl.next(self.run)
        old = s['gate']['token']
        self.ctl.revise(self.run,'researcher','Try again')
        with self.assertRaises(WorkflowError):
            self.ctl.approve(self.run, old)
        s = self.ctl.next(self.run)
        self.assertNotEqual(old, s['gate']['token'])

    def test_blocked_role_cannot_be_approved(self):
        self.provider.fail.add('tester')
        self.route(['quality'])
        s = self.ctl.next(self.run)
        self.assertEqual(s['results']['quality']['report']['status'],'blocked')
        with self.assertRaises(WorkflowError):
            self.ctl.approve(self.run,s['gate']['token'])
        self.provider.fail.remove('tester')
        self.ctl.revise(self.run,'tester','Fix coverage')
        s = self.ctl.next(self.run)
        self.assertEqual(s['results']['quality']['report']['status'],'completed')
        self.assertEqual(self.provider.calls.count('tester'),2)
        self.assertEqual(self.provider.calls.count('security'),1)

    def test_context_only_selected_approved_inputs(self):
        proposal = [
            {'id':'eval_a','template':'evals','inputs':[]},
            {'id':'plan_b','template':'planning','inputs':[]},
            {'id':'dev_c','template':'development','inputs':['eval_a']},
        ]
        self.route(proposal)
        self.execute()
        self.execute()
        self.ctl.next(self.run)
        ctx = self.provider.contexts[('dev_c','developer')]
        self.assertEqual([x['stage'] for x in ctx['prior_approved_handoffs']], ['eval_a'])
        self.assertNotIn('plan_b', json.dumps(ctx,ensure_ascii=False))

    def test_module_artifacts_and_mermaid(self):
        self.route(['evals'])
        s=self.ctl.next(self.run)
        md=self.project/'.workflow'/'runs'/self.run/'modules'/'evals'/'handoff.md'
        self.assertTrue(md.exists())
        self.assertIn('```mermaid',md.read_text(encoding='utf-8'))
        self.ctl.approve(self.run,s['gate']['token'])
        self.assertIn('**Approval:** APPROVED',md.read_text(encoding='utf-8'))
        self.assertIn('G2',route_diagram(normalize_route(['evals'])))

    def test_architect_full_checklist_blocks_incomplete(self):
        self.provider.incomplete_design=True
        self.route(['system_design'],mode='full')
        s=self.ctl.next(self.run)
        self.assertEqual(s['results']['system_design']['report']['status'],'blocked')
        self.assertTrue((self.project/'.workflow'/'runs'/self.run/'modules'/'system_design'/'system-design.md').is_file())
        with self.assertRaises(WorkflowError):
            self.ctl.approve(self.run,s['gate']['token'])
        self.provider.incomplete_design=False
        self.ctl.revise(self.run,'architect','Fill scope')
        s=self.ctl.next(self.run)
        self.assertEqual(s['results']['system_design']['report']['status'],'completed')

    def test_replan_needs_approval_and_preserves_unaffected(self):
        self.route([{'id':'eval_a','template':'evals','inputs':[]},
                    {'id':'plan_b','template':'planning','inputs':['eval_a']},
                    {'id':'dev_c','template':'development','inputs':['plan_b']}])
        self.execute()
        self.execute()
        before=list(self.provider.calls)
        s=self.ctl.propose_replan(self.run,{'modules':[
            {'id':'eval_a','template':'evals','inputs':[]},
            {'id':'plan_b','template':'planning','goal':'Новый план реализации','inputs':['eval_a']},
            {'id':'dev_c','template':'development','inputs':['plan_b']},
        ],'system_design_mode':'none'},'Поменять только план')
        self.assertEqual(s['status'],'await_replan')
        self.assertIn('eval_a',s['pending_replan']['retained_approved'])
        self.assertIn('plan_b',s['pending_replan']['approved_to_reopen'])
        with self.assertRaises(WorkflowError):
            self.ctl.next(self.run)
        s=self.ctl.approve(self.run,s['gate']['token'])
        self.assertTrue(s['results']['eval_a']['approved'])
        self.assertNotIn('plan_b',s['results'])
        self.assertEqual(self.provider.calls,before)
        self.assertEqual(self.ctl.current(s)['id'],'plan_b')

    def test_broken_assumptions_require_note(self):
        s=self.ctl.next(self.run)
        s['understanding']['assumptions']=['Not clear']
        self.store.save(s,'manual_test_assumption')
        with self.assertRaises(WorkflowError):
            self.ctl.approve(self.run,s['gate']['token'])
        self.ctl.approve(self.run,s['gate']['token'],'подтверждаю допущение')

    def test_recover_needs_inspection(self):
        self.route(['evals'])
        s=self.ctl.load(self.run)
        s['status']='running_module'
        s['running_owner']={'pid':99999999,'hostname':'test-host'}
        self.store.save(s,'simulate_crash')
        with self.assertRaises(WorkflowError):
            self.ctl.recover(self.run,confirmed=False)
        s=self.ctl.recover(self.run,confirmed=True)
        self.assertEqual(s['status'],'ready_module')

    def test_idea_path_restrictions(self):
        idea=self.project/'ideas'/'one.md'
        idea.write_text('Something',encoding='utf-8')
        self.assertIn('Something',load_idea(self.project,'ideas/one.md'))
        with self.assertRaises(WorkflowError):
            load_idea(self.project,'../escape.md')
        with self.assertRaises(WorkflowError):
            load_idea(self.project,'/etc/passwd')
        (self.project/'ideas'/'link.md').symlink_to(idea)
        with self.assertRaises(WorkflowError):
            load_idea(self.project,'ideas/link.md')

    def test_old_state_never_automigrated(self):
        s=self.ctl.load(self.run)
        s.pop('schema_version')
        self.store.save(s,'legacy_simulation')
        with self.assertRaises(WorkflowError):
            self.ctl.load(self.run)


if __name__=='__main__':
    unittest.main()
