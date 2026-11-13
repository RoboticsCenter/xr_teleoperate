"""No real calibration: every CLI invocation runs a temporary inert fake."""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import dashboard_glove_model as model

FAKE = r'''
import json, pathlib, signal, sys, time, zipfile
base = pathlib.Path(BASE)
args = sys.argv[1:]
mode = (base/'mode').read_text()
with (base/'calls.jsonl').open('a') as log: log.write(json.dumps(args)+'\n')
models = base/'sdk/users/u_test/models'
def output(value): print(json.dumps(value), flush=True)
if args[:2] == ['user','show']:
 output({'diagnostic':'SDK diagnostic JSON before actual result'})
 output({'name':'brainco-teleop','is_current':True,'left_hand':{'calibrated':False},'right_hand':{'calibrated':(models/'right_hand.urdf').is_file()},'tactile':[]})
elif args[:2] == ['user','export']:
 if mode == 'backup_failure': sys.exit(1)
 with zipfile.ZipFile(args[2],'w') as z: z.write(models/'right_hand.urdf','right_hand.urdf')
 output({'exported':args[2]})
elif args[:2] == ['get','hand_side']:
 side = 'left' if 'WG1JA06260822033' in args else 'right'
 output({'diagnostic':'SDK diagnostic JSON before actual result'})
 output({'value':'right' if mode=='wrong_side' else side})
elif args[:2] == ['calib','hand-model']:
 side = 'left' if 'WG1JA06260822033' in args else 'right'
 def event(event, **data):
  output(dict(schema_version=2,operation='calibrate',calibration='hand_model',event=event,**data))
 def cancel(*unused):
  event('cancelled',status='cancelled');sys.exit(9)
 signal.signal(signal.SIGINT,cancel)
 event('progress',feedback={'step_name':'pinch_index','step_index':0,'step_total':6,'state':'waiting_stable','progress':.2,'variance_ok':False,'metrics':[]})
 if mode == 'cancel':
  while True: time.sleep(.03)
 time.sleep(.07)
 if mode == 'no_terminal': sys.exit(0)
 if mode == 'failure':
  event('error',status='failed',error={'kind':'pose_timeout','code':1,'message':'SDK pose timeout'});sys.exit(1)
 models.mkdir(parents=True,exist_ok=True)
 (models/(side+'_hand.urdf')).write_text('<robot name="'+side+'"><link name="palm"/></robot>')
 if mode == 'change_other': (models/'right_hand.urdf').write_text('<robot name="unexpected"/>')
 event('result',status='ok',result={'handedness':side,'sdk_user':'wrong-user' if mode=='wrong_user' else 'brainco-teleop','calibrated_urdf':str(models/(side+'_hand.urdf')),'frames_per_pose':10})
else: sys.exit(10)
'''

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sdk = self.root/'sdk'
        self.models = self.sdk/'users/u_test/models'
        self.models.mkdir(parents=True)
        (self.sdk/'users/registry.toml').write_text('current_user_id="u_test"\n[[users]]\nuser_id="u_test"\ndisplay_name="brainco-teleop"\nis_default=false\n')
        (self.models/'right_hand.urdf').write_text('<robot name="right-before"><link name="palm"/></robot>')
        (self.root/'mode').write_text('success')
        self.cli = self.root/'fake-wuji'
        self.cli.write_text('#!'+sys.executable+'\n'+FAKE.replace('BASE',repr(str(self.root))))
        self.cli.chmod(0o700)

    def manager(self, **kwargs):
        value = model.GloveModelManager(self.root, cli=self.cli, sdk_root=self.sdk, cancel_grace_s=.1, **kwargs)
        self.addCleanup(value.close)
        return value

    def wait(self, manager, predicate, side='left', timeout=5):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            state = manager.status(side)
            if predicate(state): return state
            time.sleep(.01)
        self.fail('state did not settle: '+repr(manager.status(side)))

    def calls(self):
        path = self.root/'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_construct_and_status_have_no_process_or_file_side_effect(self):
        with mock.patch.object(model.subprocess, 'Popen') as popen:
            manager = self.manager()
            self.assertFalse(manager.status()['active'])
            self.assertFalse(manager.status('left')['running'])
            popen.assert_not_called()
        self.assertFalse((self.root/'diagnostics').exists())

    def test_backup_precedes_calibration_and_result_is_verified(self):
        completed = []
        manager = self.manager(on_finish=lambda side, ok: completed.append((side,ok)))
        original_right = (self.models/'right_hand.urdf').read_bytes()
        manager.start('left')
        state = self.wait(manager, lambda s:not s['running'])
        self.assertTrue(state['completed'], state)
        self.assertTrue(state['model_verified'])
        self.assertEqual(state['exit_code'],0)
        self.assertEqual(Path(state['backup_path'],'u_test/models/right_hand.urdf').read_bytes(),original_right)
        self.assertEqual((self.models/'right_hand.urdf').read_bytes(),original_right)
        self.assertEqual([c[:2] for c in self.calls()],[['user','show'],['user','export'],['get','hand_side'],['calib','hand-model']])
        self.assertNotIn('--handedness',self.calls()[-1])
        self.assertEqual(completed,[('left',True)])

    def test_async_reservation_and_cancel_during_preparation(self):
        entered, release = threading.Event(), threading.Event()
        def before(side): entered.set();release.wait(2)
        manager=self.manager(before_start=before)
        started=time.monotonic();manager.start('left')
        self.assertLess(time.monotonic()-started,.2)
        self.assertTrue(entered.wait(1))
        self.assertTrue(manager.status()['active'])
        self.assertFalse(manager.status('right')['active'])
        with self.assertRaises(RuntimeError): manager.start('right')
        manager.cancel('left');release.set()
        state=self.wait(manager,lambda s:not s['running'])
        self.assertEqual(state['phase'],'cancelled')
        self.assertEqual(self.calls(),[])

    def test_cancel_sends_one_interrupt_and_reports_sdk_cancel(self):
        (self.root/'mode').write_text('cancel')
        manager=self.manager()
        manager.start('left')
        self.wait(manager,lambda s:s['phase']=='calibrating')
        self.assertFalse(manager.status('left')['awaiting_continue'])
        manager.cancel('left')
        state=self.wait(manager,lambda s:not s['running'])
        self.assertEqual(state['phase'],'cancelled')
        self.assertEqual(state['terminal_event']['event'],'cancelled')
        self.assertEqual(state['exit_code'],9)
        self.assertFalse((self.models/'left_hand.urdf').exists())

    def test_missing_terminal_or_wrong_user_never_claim_success(self):
        for mode in ('no_terminal','wrong_user','failure','change_other'):
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                manager=self.manager();manager.start('left')
                state=self.wait(manager,lambda s:not s['running'])
                self.assertFalse(state['completed'])
                self.assertIsNotNone(state['error'])
                manager.close()

    def test_backup_or_side_failure_never_starts_calibration(self):
        for mode in ('backup_failure','wrong_side'):
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                (self.root/'calls.jsonl').unlink(missing_ok=True)
                manager=self.manager();manager.start('left')
                state=self.wait(manager,lambda s:not s['running'])
                self.assertFalse(state['completed'])
                self.assertNotIn(['calib','hand-model'],[c[:2] for c in self.calls()])
                manager.close()

    def test_external_reader_lease_blocks_before_any_cli(self):
        (self.root/'diagnostics').mkdir()
        with (self.root/'diagnostics/dashboard-glove-right.lock').open('a+') as lease:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            manager=self.manager();manager.start('left')
            state=self.wait(manager,lambda s:not s['running'])
            self.assertFalse(state['completed'])
            self.assertEqual(self.calls(),[])

    def test_simulation_pose_workflow_never_runs_cli(self):
        finished=[]
        manager=self.manager(simulation=True,on_finish=lambda s,ok:finished.append(ok))
        manager.start('left')
        for index in range(6):
            state=self.wait(manager,lambda s:s['awaiting_continue'] and s['prompt_id']==str(index))
            with self.assertRaises(ValueError): manager.continue_calibration('left','stale')
            manager.continue_calibration('left',state['prompt_id'])
        state=self.wait(manager,lambda s:not s['running'])
        self.assertTrue(state['completed'])
        self.assertTrue(state['simulation'])
        self.assertFalse(state['model_verified'])
        self.assertEqual(self.calls(),[])
        self.assertEqual(finished,[True])

    def test_simulation_does_not_acquire_production_locks(self):
        (self.root/'diagnostics').mkdir()
        leases=[]
        try:
            for name in ('model','left','right'):
                lease=(self.root/f'diagnostics/dashboard-glove-{name}.lock').open('a+')
                leases.append(lease)
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            manager=self.manager(simulation=True)
            manager.start('left')
            self.wait(manager,lambda s:s['awaiting_continue'])
            self.assertTrue(manager.status('left')['active'])
            manager.cancel('left')
            self.wait(manager,lambda s:not s['running'])
            self.assertEqual(self.calls(),[])
        finally:
            for lease in leases: lease.close()

    def test_empty_named_profile_keeps_backup_and_can_create_first_model(self):
        import shutil
        shutil.rmtree(self.sdk/'users/u_test')
        manager=self.manager()
        manager.start('left')
        state=self.wait(manager,lambda s:not s['running'])
        self.assertTrue(state['success'],state)
        self.assertTrue(Path(state['backup_path'],'registry.toml').is_file())
        self.assertTrue(Path(state['backup_path'],'u_test').is_dir())
        self.assertIsNone(state['backup_bundle'])
        self.assertNotIn(['user','export'],[c[:2] for c in self.calls()])

    def test_finish_callback_has_no_manager_lock_and_keeps_reservation(self):
        entered, release=threading.Event(),threading.Event()
        def finish(side,ok): entered.set();release.wait(2)
        manager=self.manager(on_finish=finish)
        manager.start('left');self.assertTrue(entered.wait(3))
        self.assertTrue(manager.status()['active'])
        manager.cancel('left');release.set()
        state=self.wait(manager,lambda s:not s['running'])
        self.assertFalse(state['success'])
        self.assertEqual(state['phase'],'cancelled')

    def test_guidance_uses_sdk_pose_state_and_limits_diagnostics(self):
        instruction, diagnostics=model.guidance(dict(step_name='pinch_index',state='waiting_stable',variance_ok=False,
            metrics=[dict(finger='index',error=.001,unit='m',hint='tip distance')]*12))
        self.assertIn('食指',instruction)
        self.assertIn('移动过多',instruction)
        self.assertEqual(len(diagnostics),8)

if __name__=='__main__': unittest.main()
