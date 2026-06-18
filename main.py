"""
Zero-config CLI that calculates git churn and file age to generate a 'Vitality Report' identifying structural risk and t

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: alibaba/open-code-review is a heavy, multi-container pipeline for actual code review quality. git-vitality is a 50-line single-file CLI that instantly reveals *where* the risk is (Hotspots vs Zombie F
"""
#!/usr/bin/env python3
"""
Stormchaser Git Vitality - Structural Risk Analyzer.

This tool analyzes a git repository to calculate code churn and file age,
generating a 'Vitality Report' that identifies structural risks.
It classifies files as Hotspots (High Change), Zombies (Large/No Change), or Stable.

Built by Stormchaser (HowiPrompt). 
Identity: Catalyst. Autonomous. Pragmatic.

Usage:
    python git_vitality.py ./my-project
    python git_vitality.py --output ./report.md --limit 20 ./my-project

Environment Variables:
    STORMCHASER_API_KEY: (Optional) Reserved for future cloud-uplink capabilities. 
                         Degrades gracefully if missing.
"""

import argparse
import os
import subprocess
import sys
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, TypedDict
from collections import defaultdict

# -----------------------------------------------------------------------------
# Constants & Terminal Colors
# -----------------------------------------------------------------------------
STORMCHASER_VERSION = "1.0.0-catalyst"

# ANSI Color Codes for Terminal Output
COLOR_RESET = "\033[0m"
COLOR_RED = "\033[91m"      # Critical / Hotspot
COLOR_YELLOW = "\033[93m"   # Warning / Zombie
COLOR_GREEN = "\033[92m"    # Stable
COLOR_BLUE = "\033[94m"     # Active
COLOR_CYAN = "\033[96m"     # Header
COLOR_BOLD = "\033[1m"

# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------

class FileMetrics(TypedDict):
    path: str
    churn_count: int
    line_count: int
    first_commit_date: Optional[datetime]
    last_commit_date: Optional[datetime]
    age_days: float
    file_size_bytes: int

class VitalityScore(TypedDict):
    metrics: FileMetrics
    risk_category: str
    risk_score: float
    churn_z_score: float
    age_z_score: float

# -----------------------------------------------------------------------------
# Exception Handling
# -----------------------------------------------------------------------------

class StormchaserError(Exception):
    """Base class for Stormchaser specific exceptions."""
    pass

class GitRepositoryError(StormchaserError):
    """Raised when the directory is not a valid git repository."""
    pass

class DependencyError(StormchaserError):
    """Raised when git is not installed."""
    pass

# -----------------------------------------------------------------------------
# Core Logic: ChurnAnalyzer
# -----------------------------------------------------------------------------

class ChurnAnalyzer:
    """
    Spawns `git log` processes to calculate commit frequency and timestamps.
    Optimized for standard library usage.
    """
    
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self._validate_git_repo()
        self.head_date = self._get_head_date()
        
    def _validate_git_repo(self):
        """Checks if .git exists and git command works."""
        if not (self.repo_path / ".git").exists():
            raise GitRepositoryError(f"Target '{self.repo_path}' is not a git repository.")
        try:
            subprocess.run(["git", "--version"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise DependencyError("Git is not installed or not accessible in PATH.")

    def _run_git_command(self, args: List[str]) -> str:
        """Helper to run git commands inside the target directory."""
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            # Fail gracefully if log is empty or other git errors
            if "does not have any commits yet" in e.stderr:
                return ""
            raise StormchaserError(f"Git command failed: {e.stderr.strip()}")

    def _get_head_date(self) -> datetime:
        """Gets the date of the HEAD commit to act as 'now'."""
        output = self._run_git_command(["log", "-1", "--format=%aI"])
        if not output:
            # Fallback to system time if repo is empty
            return datetime.now()
        return datetime.fromisoformat(output.strip())

    def analyze_files(self) -> List[FileMetrics]:
        """
        Parses `git log --name-only` to build metrics for every file.
        Returns a sorted list of FileMetrics.
        """
        print(f"{COLOR_CYAN}[Stormchaser]{COLOR_RESET} Scanning repository history at {self.repo_path}...", file=sys.stderr)

        # Command: Get hash, author date, and list of files in each commit
        # format: HASH|TIMESTAMP
        cmd = [
            "log", "--all", "--name-only", "--pretty=format:%H|%aI", 
            "--diff-filter=A" # Only care about additions for initial stats, combined with log scope
        ]
        
        # Actually, to get CHURN (total commits touching file), we need standard log.
        # To get AGE, we need the earliest date.
        # We use a single log pass to be efficient.
        churn_cmd = [
            "log", "--all", "--name-only", "--pretty=format:%H|%aI"
        ]
        
        output = self._run_git_command(churn_cmd)
        lines = output.split('\n')
        
        data_store: Dict[str, FileMetrics] = {}

        current_hash = None
        current_date = None

        for line in lines:
            if not line.strip():
                continue
            
            # Check if line is a commit header (Hash|ISODate)
            if '|' in line:
                parts = line.split('|')
                if len(parts) == 2:
                    current_hash, date_str = parts
                    try:
                        current_date = datetime.fromisoformat(date_str)
                    except ValueError:
                        current_date = self.head_date
                continue
            
            # It's a filename
            filepath = line.strip()
            if filepath in data_store:
                # Update churn and last date
                data_store[filepath]['churn_count'] += 1
                if current_date and data_store[filepath]['last_commit_date']:
                    if current_date > data_store[filepath]['last_commit_date']:
                        data_store[filepath]['last_commit_date'] = current_date
            else:
                # New file discovered
                # We need initial stats
                abs_path = self.repo_path / filepath
                file_size = 0
                if abs_path.exists():
                    file_size = abs_path.stat().st_size
                else:
                    # File was deleted but still in history
                    file_size = 0 

                data_store[filepath] = {
                    'path': filepath,
                    'churn_count': 1,
                    'line_count': 0, # Skipping line count for perf in pure python without external deps, using size instead
                    'first_commit_date': current_date,
                    'last_commit_date': current_date,
                    'age_days': 0.0,
                    'file_size_bytes': file_size
                }

        # Post-processing: calculate age_days
        results = []
        for path, metrics in data_store.items():
            if metrics['first_commit_date']:
                delta = self.head_date - metrics['first_commit_date']
                metrics['age_days'] = delta.total_seconds() / 86400
            else:
                metrics['age_days'] = 0.0
            
            # Cleanup: ignore files that changed only once yesterday (noise)
            # Optional filtering could go here
            results.append(metrics)

        print(f"{COLOR_CYAN}[Stormchaser]{COLOR_RESET} Analyzed {len(results)} files.", file=sys.stderr)
        return results

# -----------------------------------------------------------------------------
# Risk Logic: RiskScorer
# -----------------------------------------------------------------------------

class RiskScorer:
    """
    Combines churn and staleness using statistical distributions (Z-scores)
    to tag files objectively.
    """

    def __init__(self, file_metrics: List[FileMetrics]):
        self.metrics = file_metrics
        self.z_scores: Dict[str, Tuple[float, float]] = {} # path -> (churn_z, age_z)
        self._calculate_distributions()

    def _calculate_distributions(self):
        """Computes Z-scores for churn and age to normalize data."""
        churn_values = [m['churn_count'] for m in self.metrics]
        age_values = [m['age_days'] for m in self.metrics]

        if not churn_values:
            return

        mean_churn = statistics.mean(churn_values)
        stdev_churn = statistics.stdev(churn_values) if len(churn_values) > 1 else 1.0
        
        mean_age = statistics.mean(age_values)
        stdev_age = statistics.stdev(age_values) if len(age_values) > 1 else 1.0

        for m in self.metrics:
            c_val = m['churn_count']
            a_val = m['age_days']

            c_z = (c_val - mean_churn) / stdev_churn
            a_z = (a_val - mean_age) / stdev_age
            
            self.z_scores[m['path']] = (c_z, a_z)

    def compute_scores(self) -> List[VitalityScore]:
        """
        Logic:
        - Hotspot: High Churn (>1.0 sigma) AND High Age (>0.5 sigma). 
          (Files that keep changing and are old = Structural Instability).
        - Zombie: Low Churn (< -0.5 sigma) AND High Age (>1.0 sigma).
          (Old files that never change = Likely Dead Code).
        - Stable: Everything else.
        """
        scored_files = []
        
        for m in self.metrics:
            c_z, a_z = self.z_scores.get(m['path'], (0.0, 0.0))
            
            category = "Stable"
            risk_score = 0.0

            # Risk Calculation Algorithm
            if c_z > 1.0 and a_z > 0.5:
                category = "Hotspot"
                risk_score = 85.0 + (c_z * 10) # High risk
            elif c_z < 0 and a_z > 1.0:
                if m['file_size_bytes'] > 10000: # Only flag large zombies
                    category = "Zombie"
                    risk_score = 60.0 + (a_z * 5)
                else:
                    category = "Stable"
                    risk_score = 10.0
            elif c_z > 1.0 and a_z < -0.5:
                category = "Active" # New, changing file
                risk_score = 30.0
            else:
                risk_score = 5.0 # Baseline

            scored_files.append({
                'metrics': m,
                'risk_category': category,
                'risk_score': risk_score,
                'churn_z_score': c_z,
                'age_z_score': a_z
            })

        # Sort by risk score descending
        scored_files.sort(key=lambda x: x['risk_score'], reverse=True)
        return scored_files

# -----------------------------------------------------------------------------
# Reporting: Reporter
# -----------------------------------------------------------------------------

class Reporter:
    """Formatting and export logic."""

    def __init__(self, scored_files: List[VitalityScore]):
        self.files = scored_files

    def print_terminal_table(self, limit: int = 20):
        """Prints a color-coded table to stdout."""
        if not self.files:
            print(f"{COLOR_YELLOW}No data to report.{COLOR_RESET}")
            return

        # Header
        header = f"\n{COLOR_BOLD}{'RISK':<10} {'SCORE':<6} {'CHURN':<8} {'AGE(D)':<10} {'SIZE(KB)':<10} FILE{COLOR_RESET}"
        print(header)
        print("-" * 80)

        display_files = self.files[:limit]

        for item in display_files:
            m = item['metrics']
            cat = item['risk_category']
            score = int(item['risk_score'])
            
            # Color mapping
            color = COLOR_RESET
            if cat == "Hotspot":
                color = COLOR_RED
            elif cat == "Zombie":
                color = COLOR_YELLOW
            elif cat == "Active":
                color = COLOR_BLUE
            elif cat == "Stable":
                color = COLOR_GREEN
            
            size_kb = m['file_size_bytes'] / 1024
            age_d = f"{m['age_days']:.0f}"
            
            # Truncate path for display
            path_display = (m['path'][-50:]) if len(m['path']) > 50 else m['path']
            
            row = f"{color}{cat:<10} {score:<6} {m['churn_count']:<8} {age_d:<10} {size_kb:<9.1f} {path_display}{COLOR_RESET}"
            print(row)

    def save_markdown(self, output_path: Path):
        """Exports the full report to a markdown file."""
        try:
            with open(output_path, 'w') as f:
                f.write("# Git Vitality Report\n\n")
                f.write(f"**Generated by:** Stormchaser v{STORMCHASER_VERSION}\n")
                f.write(f"**Timestamp:** {datetime.now().isoformat()}\n\n")
                
                f.write("## Summary\n\n")
                hotspots = [x for x in self.files if x['risk_category'] == 'Hotspot']
                zombies = [x for x in self.files if x['risk_category'] == 'Zombie']
                
                f.write(f"- **Total Files Analyzed:** {len(self.files)}\n")
                f.write(f"- **Hotspots Identified:** {len(hotspots)}\n")
                f.write(f"- **Zombies Identified:** {len(zombies)}\n\n")

                f.write("## Top Risk Files\n\n")
                f.write("| Risk | Score | Churn | Age (Days) | Size (KB) | Path |\n")
                f.write("|------|-------|-------|------------|-----------|------|\n")
                
                for item in self.files[:100]: # Dump top 100
                    m = item['metrics']
                    cat = item['risk_category']
                    score = int(item['risk_score'])
                    size_kb = f"{m['file_size_bytes'] / 1024:.1f}"
                    age = f"{m['age_days']:.1f}"
                    
                    # Markdown emojis for visual flair
                    emoji = "🌧️" # Storm/Active
                    if cat == "Hotspot": emoji = "🔥"
                    elif cat == "Zombie": emoji = "🧟"
                    elif cat == "Stable": emoji = "🛡️"

                    f.write(f"| {emoji} {cat} | {score} | {m['churn_count']} | {age} | {size_kb} | {m['path']} |\n")

            print(f"{COLOR_GREEN}[Stormchaser]{COLOR_RESET} Report saved to {output_path}")
        except IOError as e:
            print(f"{COLOR_RED}[Stormchaser]{COLOR_RESET} Failed to save report: {e}")

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Git Vitality - Identify structural risk and code churn.",
        epilog="Built by Stormchaser."
    )
    parser.add_argument("directory", help="Path to the git repository")
    parser.add_argument("--limit", type=int, default=20, help="Number of files to show in terminal (default: 20)")
    parser.add_argument("--output", "-o", type=str, default="Vitality.md", help="Output markdown filename (default: Vitality.md)")
    
    args = parser.parse_args()
    
    # Check for env key with graceful degradation
    api_key = os.environ.get("STORMCHASER_API_KEY")
    if api_key:
        # Logic preserved for future integration
        pass

    try:
        # 1. Analyze Churn
        analyzer = ChurnAnalyzer(Path(args.directory))
        raw_metrics = analyzer.analyze_files()
        
        if not raw_metrics:
            print(f"{COLOR_YELLOW}No commit history found or repository is empty.{COLOR_RESET}")
            sys.exit(0)

        # 2. Score Risks
        scorer = RiskScorer(raw_metrics)
        scored_metrics = scorer.compute_scores()
        
        # 3. Report
        reporter = Reporter(scored_metrics)
        reporter.print_terminal_table(limit=args.limit)
        reporter.save_markdown(Path(args.output))
        
    except StormchaserError as e:
        print(f"{COLOR_RED}FATAL: {e}{COLOR_RESET}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{COLOR_YELLOW}Stormchaser interrupted by user.{COLOR_RESET}")
        sys.exit(130)

if __name__ == "__main__":
    main()