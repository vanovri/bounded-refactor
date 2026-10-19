import json
import subprocess
import sys
from pathlib import Path

import pytest

from bounded_refactor.core import RefactorError, relative_is_safe, run
from test_bounded_refactor import candidate, config, copy_project, fix_attempt


@pytest.mark.parametrize('name', ['../x', '..\\x', 'C:relative.py', 'C:\\x.py', '/tmp/x', '\\x', '.', 'x:stream'])
def test_portable_path_boundaries(name):
    assert not relative_is_safe(name)


def test_duplicate_candidate_aliases_are_rejected(tmp_path):
    project = copy_project(tmp_path)
    with pytest.raises(RefactorError, match='aliases'):
        run(project, config(tmp_path), candidate(tmp_path, [{'files': {
            'pkg/mathutil.py': 'def add(a,b): return 5', 'pkg\\mathutil.py': 'def add(a,b): return 6'}}]), tmp_path / 'out')


def test_refuses_overlapping_output_and_preserves_previous_evidence(tmp_path):
    project = copy_project(tmp_path)
    with pytest.raises(RefactorError, match='overlap'):
        run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), project / 'out')
    output = tmp_path / 'out'
    output.mkdir()
    marker = output / 'keep.txt'
    marker.write_text('keep')
    with pytest.raises(RefactorError, match='never overwritten'):
        run(project, config(tmp_path), candidate(tmp_path, [fix_attempt()]), output)
    assert marker.read_text() == 'keep'


def test_tests_are_frozen_even_with_broad_allowlist(tmp_path):
    project = copy_project(tmp_path)
    report = run(project, config(tmp_path, allowlist=['**']),
                 candidate(tmp_path, [{'files': {'tests/test_math.py': 'assert True\n'}}]), tmp_path / 'out')
    assert not report['accepted']


def test_signature_change_and_invalid_syntax_are_rejected(tmp_path):
    project = copy_project(tmp_path)
    for index, code in enumerate(['def add(a):\n    return a\n', 'def add(:']):
        report = run(project, config(tmp_path), candidate(tmp_path, [{'files': {'pkg/mathutil.py': code}}]), tmp_path / f'out-{index}')
        assert not report['accepted']


def test_check_cannot_modify_frozen_files(tmp_path):
    project = copy_project(tmp_path)
    check = [sys.executable, '-c', "from pathlib import Path; Path('pkg/mathutil.py').write_text('def add(a,b): return 999')"]
    report = run(project, config(tmp_path, check_command=check), candidate(tmp_path, [fix_attempt()]), tmp_path / 'out')
    assert not report['accepted']
    assert 'frozen' in report['attempts'][0]['reason']


def test_api_paths_cannot_escape_and_missing_command_is_recorded(tmp_path):
    project = copy_project(tmp_path)
    with pytest.raises(RefactorError, match='api_paths'):
        run(project, config(tmp_path, api_paths=['../other.py']), candidate(tmp_path, [fix_attempt()]), tmp_path / 'out')
    report = run(project, config(tmp_path, check_command=['does-not-exist-1234']), candidate(tmp_path, [fix_attempt()]), tmp_path / 'out2')
    assert not report['accepted']


def test_exported_no_newline_diff_replays_with_git_apply(tmp_path):
    project = copy_project(tmp_path)
    original = project / 'pkg/mathutil.py'
    original.write_text('def add(a, b):\n    return a - b')
    attempt = {'files': {'pkg/mathutil.py': 'def add(a, b):\n    return a + b'}}
    report = run(project, config(tmp_path), candidate(tmp_path, [attempt]), tmp_path / 'out')
    assert report['accepted']
    applied = subprocess.run(['git', 'apply', '--check', str(tmp_path / 'out/accepted.diff')], cwd=project, capture_output=True)
    assert applied.returncode == 0, applied.stderr
