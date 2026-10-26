"""Standalone contracts: changes to any module can be verified in isolation."""
from pathlib import Path
import tempfile
import unittest

from workflow_engine.modules import CATALOG, INTERNAL
from workflow_engine.provider import AgentRegistry, DemoProvider, packaged_config_root
from workflow_engine.schema import WorkflowError, normalize_route
from workflow_engine.store import StateStore
from workflow_engine.controller import ModuleController


class ContractTests(unittest.TestCase):
    def test_each_module_has_own_source_contract(self):
        moddir=Path(__file__).resolve().parents[1]/'workflow_engine'/'modules'
        self.assertEqual(len(CATALOG),9)
        self.assertEqual(len(INTERNAL),2)
        for name, spec in CATALOG.items():
            with self.subTest(name=name):
                self.assertTrue((moddir/f'{name}.py').is_file())
                self.assertTrue(spec.agents)
                self.assertTrue(spec.description)

    def test_every_agent_role_exists(self):
        r=AgentRegistry(packaged_config_root())
        roles={'orchestrator',*(a for spec in CATALOG.values() for a in spec.agents)}
        self.assertEqual(len(roles),13)
        for role in roles:
            with self.subTest(role=role):
                self.assertTrue(r.role(role).startswith('---'))

    def test_each_module_runs_without_other_specialists(self):
        for name in CATALOG:
            with self.subTest(module=name), tempfile.TemporaryDirectory() as td:
                project=Path(td)
                store=StateStore(project/'.workflow'/'state.db')
                try:
                    ctrl=ModuleController(store, AgentRegistry(packaged_config_root()), DemoProvider())
                    run=ctrl.new(project,'Проверить выполнение отдельного независимого модуля')['id']
                    s=ctrl.next(run);ctrl.approve(run,s['gate']['token'])
                    s=ctrl.next(run)
                    mode='light' if name=='system_design' else 'none'
                    custom=[{'id':name,'template':name,'goal':'Самостоятельная проверка модуля','inputs':[]}]
                    s=ctrl.set_route(run,{'modules':custom,'system_design_mode':mode})
                    ctrl.approve(run,s['gate']['token'])
                    s=ctrl.next(run)
                    self.assertEqual(set(s['results'][name]['agent_results']),set(CATALOG[name].agents))
                    self.assertEqual(set(s['results']),{name})
                    self.assertEqual(s['results'][name]['report']['status'],'completed')
                    ctrl.approve(run,s['gate']['token'])
                    if name != 'documentation':
                        s=ctrl.next(run)
                        self.assertEqual(set(s['results']['documentation']['agent_results']),{'documentation'})
                finally:
                    store.close()

    def test_invalid_design_modes(self):
        for route,mode in [(['system_design'],'none'),(['evals'],'full'),(['evals'],'wrong')]:
            with self.subTest(route=route,mode=mode):
                with self.assertRaises(WorkflowError):
                    normalize_route(route,mode)

    def test_cannot_reuse_approval_token_between_gate_versions(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);store=StateStore(p/'.workflow'/'state.db')
            try:
                ctrl=ModuleController(store,AgentRegistry(packaged_config_root()),DemoProvider())
                run=ctrl.new(p,'Создать тестовое приложение с регистрацией и тестами')['id']
                first=ctrl.next(run)
                token=first['gate']['token']
                ctrl.answer(run,'Добавить двухфакторный вход')
                revised=ctrl.next(run)
                self.assertNotEqual(token,revised['gate']['token'])
                with self.assertRaises(WorkflowError):
                    ctrl.approve(run,token)
            finally:
                store.close()

    def test_sqlite_stale_save_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'.workflow'/'state.db'
            a=StateStore(p);b=StateStore(p)
            try:
                ctrl=ModuleController(a,AgentRegistry(packaged_config_root()),DemoProvider())
                run=ctrl.new(p.parents[1],'Написать тестовый сценарий авторизации')['id']
                old=b.get(run)
                new=a.get(run);new['status']='understanding_draft'
                a.save(new,'writer_a')
                with self.assertRaises(WorkflowError):
                    b.save(old,'writer_b')
            finally:
                a.close();b.close()


if __name__=='__main__':
    unittest.main()
