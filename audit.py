"""Audit public observations and notify through GitHub's existing email settings."""
import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
MINUTES = {7, 17, 27, 37, 47, 57}
TITLE = '전기차 수집 정기 점검 보고'


def parse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(KST)


def slots_between(start, end):
    cursor = start.replace(second=0, microsecond=0)
    result = []
    while cursor <= end:
        if start < cursor and cursor.minute in MINUTES:
            result.append(cursor)
        cursor += timedelta(minutes=1)
    return result


def report_cutoff(now, event):
    if event != 'schedule':
        return now
    candidates = [now.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(days=d)
                  for d in (0, 1) for h, m in ((6, 30), (12, 0), (22, 0))]
    return max(t for t in candidates if t <= now)


def github(repo, path, payload=None):
    url = 'https://api.github.com/repos/' + repo + path
    req = urllib.request.Request(url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None,
        headers={'Accept': 'application/vnd.github+json',
                 'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                 'X-GitHub-Api-Version': '2022-11-28',
                 'User-Agent': 'EV-observation-audit', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    now = datetime.now(KST)
    repo = os.environ['GITHUB_REPOSITORY']
    owner = repo.split('/')[0]
    root = Path('reports')
    root.mkdir(exist_ok=True)
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    end = report_cutoff(now, os.environ.get('GITHUB_EVENT_NAME'))
    snapshot_files = sorted(Path('data/snapshots').glob('*/*.json.gz'))
    if not snapshot_files:
        raise RuntimeError('NoSnapshotsAvailableForAudit')
    with gzip.open(snapshot_files[0], 'rt', encoding='utf-8') as stream:
        first = parse(json.load(stream)['metadata']['started_at_kst'])
    start = parse(state['last_report_cutoff_kst']) if state.get('last_report_cutoff_kst') else first
    # A manual run must not advance the regular reporting checkpoint.
    manual = os.environ.get('GITHUB_EVENT_NAME') != 'schedule'
    if end <= start:
        raise RuntimeError('DuplicateOrOutOfOrderReport')
    observations = []
    corrupt_files = []
    for path in snapshot_files:
        if path.parent.name < start.date().isoformat() or path.parent.name > end.date().isoformat():
            continue
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                doc = json.load(stream)
            meta = doc['metadata']
            moment = parse(meta['started_at_kst'])
            if not start <= moment <= end:
                continue
            core_missing = sum(any(not r.get(k) for k in ('statId', 'chgerId', 'stat', 'statUpdDt'))
                               for r in doc['records'])
            stale = 0
            invalid_update = 0
            for row in doc['records']:
                try:
                    updated = datetime.strptime(row.get('statUpdDt', ''), '%Y%m%d%H%M%S').replace(tzinfo=KST)
                    stale += (moment - updated).total_seconds() > 86400
                except (ValueError, TypeError):
                    invalid_update += 1
            observations.append({'file': str(path), 'meta': meta, 'time': moment,
                                 'run_id': int(path.name.split('-')[-2]),
                                 'core_missing_rows': core_missing,
                                 'stale_update_rows': stale,
                                 'invalid_update_rows': invalid_update,
                                 'ids': {(r.get('statId'), r.get('chgerId')) for r in doc['records']}})
        except (ValueError, KeyError, OSError, TypeError):
            corrupt_files.append(str(path))
    observations.sort(key=lambda o: o['time'])
    runs = []
    run_scan_complete = False
    for page in range(1, 11):
        chunk = github(repo, '/actions/workflows/collect.yml/runs?per_page=100&page=' + str(page))['workflow_runs']
        runs.extend(chunk)
        if len(chunk) < 100 or (chunk and parse(chunk[-1]['created_at']) < start - timedelta(days=1)):
            run_scan_complete = True
            break
    run_by_id = {r['id']: r for r in runs}
    scheduled_runs = [r for r in runs if r['event'] == 'schedule'
                      and start < parse(r['created_at']) <= end]
    expected = slots_between(start, end)
    scheduled_observations = [o for o in observations
                              if run_by_id.get(o['run_id'], {}).get('event') == 'schedule']
    complete = [o for o in scheduled_observations if o['meta'].get('complete')]
    mature_expected = [t for t in expected if now - t >= timedelta(minutes=20)]
    deficit = max(0, len(mature_expected) - len(complete))
    saved_ids = {o['run_id'] for o in observations}
    unsaved_runs = [r for r in scheduled_runs if r['id'] not in saved_ids]
    lines = [f"@{owner}", '', f"## 전기차 수집 점검 ({'시험 발송' if manual else '정기 보고'})",
             f"- 기준 구간: {start:%Y-%m-%d %H:%M:%S} 초과 ~ {end:%Y-%m-%d %H:%M:%S} 이하 (한국시간)",
             f"- 실제 점검 시각: {now:%Y-%m-%d %H:%M:%S} (한국시간)",
             '- 계획: 매시 07·17·27·37·47·57분. 최초 수집 시작 전 시각은 제외.', '',
             '### 예정과 실제',
             f'- 예정 수집: {len(expected)}회 (이 중 20분 이상 지난 예정: {len(mature_expected)}회)',
             f'- 해당 구간에 생성된 예약 실행: {len(scheduled_runs)}회',
             f'- 해당 구간에 시작하여 파일이 저장된 예약 관측: {len(scheduled_observations)}회',
             f'- 완전 수집 표시가 있는 예약 관측: {len(complete)}회',
             f'- 충분히 기다린 예정 대비 완전 관측 부족: {deficit}회 (누락 의심, 아래 한계 참조)',
             f'- 실행은 있으나 해당 구간의 저장 파일을 확인하지 못한 회차: {len(unsaved_runs)}회',
             f'- 읽기 실패한 관측 파일: {len(corrupt_files)}개',
             f'- 실행 기록 조회 범위 충분 여부: {run_scan_complete}', '',
             '**예정 시각**', ', '.join(t.strftime('%m/%d %H:%M') for t in expected) or '없음', '',
             '**실제 예약 실행**']
    for run in sorted(scheduled_runs, key=lambda r: r['created_at']):
        started = parse(run.get('run_started_at') or run['created_at'])
        lines.append(f"- {started:%m/%d %H:%M:%S}: {run['status']} / {run.get('conclusion')} "
                     f"[실행 {run['run_number']}]({run['html_url']})")
    if not scheduled_runs:
        lines.append('- 조회 구간에서 확인되지 않음')
    lines.extend(['', '### 실제 저장 시각과 수집 범위',
                  '| 조회 시작(KST) | 저장 기록/충전소 | 완전 수집 | 종류 |',
                  '|---|---:|---|---|'])
    for item in observations:
        meta = item['meta']
        event = run_by_id.get(item['run_id'], {}).get('event', '확인 불가')
        lines.append(f"| {item['time']:%m/%d %H:%M:%S} | {meta.get('gumi_rows_retained', 0)} / "
                     f"{meta.get('gumi_stations_retained', 0)} | {meta.get('complete')} | {event} |")
    lines.extend(['', '### 데이터 이상 징후',
                  f"- 필수 항목 누락 관측행: {sum(o['core_missing_rows'] for o in observations)}",
                  f"- 갱신 시각 형식 이상 관측행: {sum(o['invalid_update_rows'] for o in observations)}",
                  f"- 원본 상태 갱신이 24시간 초과된 관측행: {sum(o['stale_update_rows'] for o in observations)}",
                  '- 위 행 수는 여러 시점에 반복 등장한 충전기를 중복 집계한 값입니다.'])
    for previous, current in zip(observations, observations[1:]):
        gap = (current['time'] - previous['time']).total_seconds() / 60
        if gap > 15:
            lines.append(f"- 관측 공백: {previous['time']:%m/%d %H:%M:%S} ~ "
                         f"{current['time']:%m/%d %H:%M:%S} ({gap:.1f}분)")
        if previous['meta'].get('complete') and current['meta'].get('complete'):
            absent = previous['ids'] - current['ids']
            if absent:
                lines.append(f"- {current['time']:%m/%d %H:%M}: 직전 완전 관측에 있던 충전기 "
                             f"{len(absent)}개가 이번 목록에 없음. 삭제/변경/누락 원인 미확인.")
    if observations:
        gap = (end - observations[-1]['time']).total_seconds() / 60
        lines.append(f'- 기준 시각까지 마지막 저장 관측 이후 공백: {gap:.1f}분')
    else:
        lines.append('- 이 구간에 저장 관측이 없습니다.')
    lines.extend(['', '### 해석상 주의',
                  '- 예약 시각과 지연 실행의 정확한 일대일 대응은 GitHub 기록만으로 확정하지 않습니다. 예정/실제 시각을 따로 제시합니다.',
                  '- 최근 20분의 부족은 대기/지연일 수 있으며, 부족 횟수도 확정 누락이 아닌 의심 수치입니다.',
                  '- 파일은 실제 저장된 관측입니다. 실행 실패/미실행 구간을 정상 데이터로 채우지 않습니다.',
                  '- 오래 갱신되지 않은 상태나 충전기 수 감소만으로 고장/누락을 확정하지 않습니다.',
                  '- 관측 사이에 시작하고 끝난 짧은 충전이나 제공기관이 누락한 이력의 존재는 이 점검으로 검출할 수 없습니다.',
                  '- 보고 작업 자체도 GitHub 사정으로 지연될 수 있습니다.',
                  '', '출처: 한국환경공단 공개 API, 공공누리 제1유형(출처표시).',
                  'https://www.data.go.kr/data/15076352/openapi.do'])
    body = '\n'.join(lines)
    stamp = now.strftime('%Y-%m-%d_%H%M%S')
    (root / (stamp + '.md')).write_text(body, encoding='utf-8')
    (root / 'latest.md').write_text(body, encoding='utf-8')
    issues = github(repo, '/issues?state=all&per_page=100')
    issue = next((x for x in issues if x['title'] == TITLE and 'pull_request' not in x), None)
    if issue:
        if issue['state'] != 'open':
            raise RuntimeError('ReportIssueClosed')
        result = github(repo, f"/issues/{issue['number']}/comments", {'body': body})
        report_url = result['html_url']
    else:
        result = github(repo, '/issues', {'title': TITLE, 'body': body})
        report_url = result['html_url']
    state['last_report_url'] = report_url
    state['last_generated_at_kst'] = now.isoformat()
    if not manual:
        state['last_report_cutoff_kst'] = end.isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
        stream.write(body + '\n\nNotification posted: ' + report_url)
    print('Audit notification posted:', report_url)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never log request objects, bearer tokens or exception response bodies.
        print('Audit failed:', type(exc).__name__)
        sys.exit(1)
