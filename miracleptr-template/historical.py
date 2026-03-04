import subprocess
import os
import json
import shutil
import sys
from datetime import datetime

def subtract_months(dt, months):
    """Helper to safely subtract months from a datetime object."""
    month = dt.month - months
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    # clamp day to valid range for the target month
    day = min(dt.day, [31, 29 if year % 4 == 0 and (not year % 100 == 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return dt.replace(year=year, month=month, day=day)

def main():
    print("Starting historical data generation...")
    
    # 1. Identify starting state to safely restore later
    try:
        original_branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).decode().strip()
        if original_branch == 'HEAD':
            # Detached head fallback
            original_branch = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
    except subprocess.CalledProcessError:
        print("Error: Must be run inside a Git repository.")
        return

    index_entries = []
    current_date = datetime.now()

    try:
        # 3 years = 36 months / 3 months = 12 steps (plus step 0 = current)
        for step in range(13):
            target_date = subtract_months(current_date, step * 3)
            date_label = target_date.strftime('%Y-%m')
            
            # Format date for Git before-query (e.g. 2025-05-01 23:59:59)
            git_date_str = target_date.strftime('%Y-%m-%d 23:59:59')
            
            # Find the last commit before this target date
            try:
                commit_cmd = ['git', 'rev-list', '-1', f'--before="{git_date_str}"', original_branch]
                commit_hash = subprocess.check_output(commit_cmd).decode().strip()
            except subprocess.CalledProcessError:
                print(f"[{date_label}] No commit found before {git_date_str}. Stopping history traversal.")
                break
                
            if not commit_hash:
                print(f"[{date_label}] Reached end of history before {git_date_str}. Stopping.")
                break

            csv_filename = f"data_{date_label.replace('-', '_')}.csv"
            
            print(f"[{date_label}] Checking out commit {commit_hash[:8]}...")
            subprocess.run(['git', 'checkout', '-q', commit_hash], check=True)
            
            print(f"[{date_label}] Running script.py...")
            try:
                # Use sys.executable to ensure we use the same Python environment (venv)
                subprocess.run([sys.executable, 'script.py'], check=True)
                
                # script.py generates 'template_member_counts.csv' by default
                if os.path.exists('template_member_counts.csv'):
                    shutil.move('template_member_counts.csv', csv_filename)
                    
                    # Append to our tracking array for the JSON output
                    index_entries.append({
                        "date": date_label,
                        "file": csv_filename
                    })
                else:
                    print(f"[{date_label}] Warning: script.py did not produce 'template_member_counts.csv'")
            except subprocess.CalledProcessError as e:
                print(f"[{date_label}] Error running script.py: {e}")

    except KeyboardInterrupt:
        print("\n\nProcess interrupted by user! Halting history traversal.")

    finally:
        # 2. Restore repository to its original state (always executes, even on Ctrl+C)
        print(f"\nRestoring repository to {original_branch}...")
        subprocess.run(['git', 'checkout', '-q', original_branch], check=True)
        
        # 3. Write index.json (saves whatever progress was made before interrupt)
        if index_entries:
            print("Writing index.json mapping file...")
            with open('index.json', 'w') as f:
                json.dump(index_entries, f, indent=4)
            print("\nProcess complete!")
        else:
            print("\nProcess finished without generating any data.")

if __name__ == "__main__":
    main()
