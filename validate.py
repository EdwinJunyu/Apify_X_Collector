if __name__ == '__main__':
    import platform
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    output = ['Environment: ' + platform.platform(), 'Python: ' + sys.version, '']
    success = True
    for directory in ('tests', 'legacy/apify_x_probe_v0_1/tests'):
        command = [sys.executable, '-m', 'unittest', 'discover', '-s', directory, '-v']
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding='utf-8')
        output.append('Suite: ' + directory)
        output.append(result.stdout)
        output.append(result.stderr)
        if result.returncode != 0:
            success = False
    report = '\n'.join(output)
    (root / 'docs' / 'TEST_RESULTS.txt').write_text(report, encoding='utf-8')
    print(report)
    raise SystemExit(0 if success else 1)
