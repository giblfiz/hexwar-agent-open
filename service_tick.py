#!/usr/bin/env python3
"""One bounded scheduled event tick; no LLM calls, shell execution, or payments."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

import deliver
import event_operator as op
from moltbook import MoltbookClient


def run(root):
    root=Path(root).resolve()
    now=datetime.now(timezone.utc)
    result={'observed_at':op._iso(now),'funds_transferred':False}
    try:
        result['delivery']=deliver.tick(root,now=now)
        if result['delivery']['phase'] in ('stopped','delivery_horizon_ended'):
            op._write(root/'.operator'/'last-tick.json',result)
            print(json.dumps(result))
            return 0
        launch=op._read(root/'outbox'/'00-launch.json')
        if launch['state']=='published':
            cfg=op._read(root/'operator-config.json')
            post_id=launch['publication_id']
            if not cfg['registration_post_id']:
                # Verify our exact announced text is publicly readable before opening.
                post=MoltbookClient().fetch_post(post_id)
                if post['content']!=launch['content']:raise ValueError('announcement mismatch')
                cfg['registration_post_id']=post_id;op._write(root/'operator-config.json',cfg)
            elif cfg['registration_post_id']!=post_id:raise ValueError('announcement ID changed')
            result['operator']=op.poll(root,now=now)
        else:result['operator']={'phase':'awaiting_registration_announcement'}
    except Exception as exc:
        result['error_code']=type(exc).__name__
    op._write(root/'.operator'/'last-tick.json',result)
    print(json.dumps(result))
    return 1 if 'error_code' in result else 0

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--event-dir',required=True,type=Path)
    raise SystemExit(run(p.parse_args().event_dir))
