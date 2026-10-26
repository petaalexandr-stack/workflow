"""Offline app-server integration. Never uses auth, network or real models."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_engine.codex.client import split_codex_command
from workflow_engine.codex.provider import CodexProvider
from workflow_engine.codex.threads import ThreadStore
from workflow_engine.controller import ModuleController
from workflow_engine.provider import AgentRegistry, ApprovalOnlyProvider, packaged_config_root
from workflow_engine.schema import WorkflowError
from workflow_engine.store import StateStore

FAKE = Path(__file__).with_name('fake_codex_app_server.py')


class CodexCommandParsingTests(unittest.TestCase):
    def test_windows_backslash_paths_are_preserved(self):
        cmd = r'C:\tools\codex.exe D:\project\script.py'
        self.assertEqual(split_codex_command(cmd, windows=True),
                         [r'C:\tools\codex.exe', r'D:\project\script.py'])

    def test_windows_quoted_path_with_spaces(self):
        cmd = r'"C:\Program Files\Codex\codex.exe" --version'
        self.assertEqual(split_codex_command(cmd, windows=True),
                         [r'C:\Program Files\Codex\codex.exe', '--version'])


class BridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            'WORKFLOW_CODEX_COMMAND': f'{sys.executable} {FAKE}',
            'FAKE_CODEX_STATE': str(self.project/'fake_state.json')
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = StateStore(self.project/'.workflow'/'state.db')
        self.addCleanup(self.store.close)
        self.registry = AgentRegistry(packaged_config_root())
        self.shell = ModuleController(self.store,self.registry,ApprovalOnlyProvider())
        self.run = self.shell.new(self.project,'Создать систему уведомлений в приложении')['id']
        state=self.shell.load(self.run)
        state['provider']='codex'
        self.store.save(state,'test_set_codex')

    def worker(self):
        return ModuleController(self.store,self.registry,CodexProvider(self.project,self.run,timeout=20))

    def thread_store(self):
        return ThreadStore(self.project/'.workflow'/'codex_threads.db')

    def test_full_codex_named_chats_with_gates(self):
        ctl=self.worker()
        s=ctl.next(self.run)
        self.assertEqual(s['status'],'await_understanding')
        self.assertEqual(len(self.thread_store().list_threads(self.run)),1)
        self.assertEqual(self.thread_store().list_threads(self.run)[0]['role'],'orchestrator')
        with self.assertRaises(WorkflowError):
            ctl.next(self.run)
        self.shell.approve(self.run,s['gate']['token'])
        s=ctl.next(self.run)
        self.assertEqual(s['status'],'await_route')
        self.assertEqual(len(self.thread_store().list_threads(self.run)),1)
        self.shell.approve(self.run,s['gate']['token'])
        s=ctl.next(self.run)
        self.assertEqual(s['status'],'await_module_review')
        self.assertEqual(len(self.thread_store().list_threads(self.run)),2)
        self.shell.approve(self.run,s['gate']['token'])
        s=ctl.next(self.run)
        self.assertEqual(s['results']['documentation']['report']['status'],'completed')
        self.shell.approve(self.run,s['gate']['token'])
        final=self.shell.load(self.run)
        self.assertEqual(final['status'],'completed')
        threads=self.thread_store().list_threads(self.run)
        self.assertEqual(len(threads),3)
        self.assertTrue(all(row['title'].startswith('WF-') for row in threads))
        hist=json.loads((self.project/'fake_state.json').read_text())['history']
        self.assertTrue(any(item['method']=='thread/resume' for item in hist))
        self.assertTrue(all(item['sandbox']['type']=='readOnly' for item in hist if item['method']=='turn/start'))
        self.assertEqual(len(list((self.project/'.workflow'/'runs'/self.run/'modules').rglob('handoff.md'))),2)

    def test_declines_app_server_approval_request(self):
        with patch.dict(os.environ,{'FAKE_CODEX_REQUEST_APPROVAL':'1'}):
            s=self.worker().next(self.run)
        self.assertEqual(s['status'],'await_understanding')
        hist=json.loads((self.project/'fake_state.json').read_text())
        self.assertEqual(hist['approval_responses'][0]['result'], {'decision':'decline'})

    def test_uncertain_turn_stays_pending(self):
        with patch.dict(os.environ,{'FAKE_CODEX_DISCONNECT':'1'}):
            with self.assertRaises(WorkflowError):
                self.worker().next(self.run)
        item=self.thread_store().lookup(self.run,'__main__','orchestrator')
        self.assertEqual(item['pending'],1)
        with self.assertRaises(WorkflowError):
            self.worker().next(self.run)
        self.thread_store().clear_pending(self.run,'__main__','orchestrator')
        s=self.worker().next(self.run)
        self.assertEqual(s['status'],'await_understanding')

    def test_revision_uses_same_codex_thread(self):
        ctl=self.worker()
        s=ctl.next(self.run)
        self.shell.approve(self.run,s['gate']['token'])
        s=ctl.next(self.run)
        self.shell.approve(self.run,s['gate']['token'])
        ctl.next(self.run)
        thread1=self.thread_store().lookup(self.run,'evals','evals')['thread_id']
        self.shell.revise(self.run,'evals','Исправить бинарный критерий')
        ctl.next(self.run)
        thread2=self.thread_store().lookup(self.run,'evals','evals')['thread_id']
        self.assertEqual(thread1,thread2)
        hist=json.loads((self.project/'fake_state.json').read_text())['history']
        starts=[it for it in hist if it['method']=='turn/start' and it['threadId']==thread1]
        self.assertEqual(len(starts),2)


if __name__=='__main__':
    unittest.main()
