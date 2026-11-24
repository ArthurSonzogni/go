import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime


def subtract_months(dt, months):
    """Safely subtract months from a datetime object."""
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    days_in_month = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ][month - 1]
    return dt.replace(year=year, month=month, day=min(dt.day, days_in_month))


def main():
    parser = argparse.ArgumentParser(
        description='Generate monthly historical CSV snapshots using script.py --rev.'
    )
    parser.add_argument(
        '--repo',
        default='.',
        help='Path to Chromium git repository.',
    )
    parser.add_argument(
        '--out-dir',
        default='.',
        help='Directory to write data_YYYY_MM.csv and index.json.',
    )
    parser.add_argument(
        '--start',
        default='2023-03',
        help='Earliest YYYY-MM snapshot to generate (inclusive).',
    )
    parser.add_argument(
        '--step-months',
        type=int,
        default=1,
        help='Months between snapshots (default: 1 = monthly).',
    )
    args = parser.parse_args()

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'script.py')
    os.makedirs(args.out_dir, exist_ok=True)

    current_date = datetime.now()
    index_entries = []

    # Step 0: Today (HEAD)
    head_hash = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=args.repo, text=True
    ).strip()
    today_csv = os.path.join(args.out_dir, 'data_today.csv')
    today_instances = os.path.join(args.out_dir, 'instances_today.csv')
    print(f"[Today] Scanning HEAD ({head_hash[:8]})...")
    subprocess.run(
        [
            sys.executable, script_path,
            '--repo', args.repo,
            '--rev', head_hash,
            '--counts-out', today_csv,
            '--instances-out', today_instances,
        ],
        check=True,
    )
    shutil.copyfile(today_csv, os.path.join(args.out_dir, 'data.csv'))
    shutil.copyfile(today_instances, os.path.join(args.out_dir, 'instances.csv'))
    index_entries.append({
        'date': 'Today',
        'full_date': current_date.strftime('%Y-%m-%d'),
        'file': 'data_today.csv',
    })

    # Monthly steps going backwards from the 1st of the previous month down to args.start
    first_of_this_month = current_date.replace(day=1)
    step = args.step_months
    while True:
        target_date = subtract_months(first_of_this_month, step)
        date_label = target_date.strftime('%Y-%m')
        if date_label < args.start:
            break

        git_date_str = target_date.strftime('%Y-%m-%d 23:59:59')
        commit_hash = subprocess.check_output(
            ['git', 'rev-list', '-1', f'--before={git_date_str}', 'HEAD'],
            cwd=args.repo,
            text=True,
        ).strip()
        if not commit_hash:
            print(f"[{date_label}] No commit found before {git_date_str}. Stopping.")
            break

        csv_filename = f"data_{date_label.replace('-', '_')}.csv"
        csv_path = os.path.join(args.out_dir, csv_filename)
        print(f"[{date_label}] Scanning commit {commit_hash[:8]} (before {git_date_str})...")
        subprocess.run(
            [
                sys.executable, script_path,
                '--repo', args.repo,
                '--rev', commit_hash,
                '--counts-out', csv_path,
                '--instances-out', '',
            ],
            check=True,
        )
        index_entries.append({
            'date': date_label,
            'file': csv_filename,
        })
        step += args.step_months

    index_path = os.path.join(args.out_dir, 'index.json')
    with open(index_path, 'w', encoding='utf8') as f:
        json.dump(index_entries, f, indent=4)
    print(f"\nDone! Generated {len(index_entries)} snapshots and updated {index_path}.")


if __name__ == '__main__':
    main()
