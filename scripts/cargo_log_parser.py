#!/usr/bin/env python3
"""
Cargo Build Log Parser

A comprehensive parser for cargo build logs that extracts errors, warnings,
and notes with full context. Provides regex-based filtering by file path,
error message, error code, and more.

Supports stdin for piping directly from cargo build:
    cargo build 2>&1 | cargo_log_parser.py --errors --file "tests/.*"

Designed for LLM consumption with structured output options.
"""

import re
import sys
import json
import argparse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, TextIO, Callable
from enum import Enum


class DiagnosticLevel(Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"
    HELP = "help"
    INFO = "info"


@dataclass
class SourceLocation:
    """Represents a location in source code."""
    file_path: str
    line: Optional[int] = None
    column: Optional[int] = None
    end_line: Optional[int] = None
    end_column: Optional[int] = None

    def matches_file_regex(self, pattern: str) -> bool:
        """Check if file path matches regex pattern."""
        return bool(re.search(pattern, self.file_path))

    def __str__(self) -> str:
        if self.line is not None:
            if self.column is not None:
                return f"{self.file_path}:{self.line}:{self.column}"
            return f"{self.file_path}:{self.line}"
        return self.file_path


@dataclass
class Diagnostic:
    """Represents a single diagnostic (error/warning/note) from cargo."""
    level: DiagnosticLevel
    message: str
    code: Optional[str] = None  # e.g., E0425, E0308
    location: Optional[SourceLocation] = None
    raw_text: str = ""  # The complete raw text block
    line_start: int = 0  # Line number in log file where this starts
    line_end: int = 0  # Line number in log file where this ends
    children: list = field(default_factory=list)  # Sub-diagnostics (notes, help)
    context_lines: list = field(default_factory=list)  # Source code context

    def matches_message_regex(self, pattern: str) -> bool:
        """Check if message matches regex pattern."""
        return bool(re.search(pattern, self.message))

    def matches_code(self, code: str) -> bool:
        """Check if error code matches (exact or pattern)."""
        if self.code is None:
            return False
        return bool(re.search(code, self.code))

    def matches_file_regex(self, pattern: str) -> bool:
        """Check if any location matches the file pattern."""
        if self.location and self.location.matches_file_regex(pattern):
            return True
        for child in self.children:
            if child.location and child.location.matches_file_regex(pattern):
                return True
        return False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "level": self.level.value,
            "message": self.message,
            "code": self.code,
            "location": str(self.location) if self.location else None,
            "log_lines": f"{self.line_start}-{self.line_end}",
            "raw_text": self.raw_text,
        }
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        if self.context_lines:
            result["context"] = self.context_lines
        return result

    def summary(self) -> str:
        """Return a brief summary for listing."""
        loc = f" at {self.location}" if self.location else ""
        code = f"[{self.code}]" if self.code else ""
        return f"{self.level.value}{code}: {self.message}{loc}"


@dataclass
class ParsedLog:
    """Container for all parsed diagnostics from a cargo log."""
    file_path: str
    diagnostics: list = field(default_factory=list)
    raw_lines: list = field(default_factory=list)

    @property
    def errors(self) -> list:
        return [d for d in self.diagnostics if d.level == DiagnosticLevel.ERROR]

    @property
    def warnings(self) -> list:
        return [d for d in self.diagnostics if d.level == DiagnosticLevel.WARNING]

    @property
    def notes(self) -> list:
        return [d for d in self.diagnostics if d.level == DiagnosticLevel.NOTE]

    def filter(
        self,
        level: Optional[DiagnosticLevel] = None,
        file_pattern: Optional[str] = None,
        message_pattern: Optional[str] = None,
        code_pattern: Optional[str] = None,
    ) -> list:
        """Filter diagnostics by various criteria."""
        results = self.diagnostics

        if level is not None:
            results = [d for d in results if d.level == level]

        if file_pattern is not None:
            results = [d for d in results if d.matches_file_regex(file_pattern)]

        if message_pattern is not None:
            results = [d for d in results if d.matches_message_regex(message_pattern)]

        if code_pattern is not None:
            results = [d for d in results if d.matches_code(code_pattern)]

        return results

    def get_log_slice(self, line_start: int, line_end: int) -> str:
        """Get a slice of the raw log by line numbers."""
        return "\n".join(self.raw_lines[line_start:line_end + 1])

    def summary(self) -> dict:
        """Return a summary of the parsed log."""
        return {
            "file": self.file_path,
            "total_diagnostics": len(self.diagnostics),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "notes": len(self.notes),
            "error_codes": list(set(d.code for d in self.errors if d.code)),
            "warning_codes": list(set(d.code for d in self.warnings if d.code)),
            "affected_files": list(set(
                d.location.file_path for d in self.diagnostics
                if d.location
            )),
        }


class CargoLogParser:
    """
    Parser for cargo build log files.
    
    Handles the standard cargo output format including:
    - error[E0XXX]: message
    - warning[lint_name]: message
    - note: message
    - help: message
    - Source code snippets with line numbers
    - Multi-line diagnostics with proper boundaries
    """

    # Pattern for the start of a diagnostic
    DIAGNOSTIC_HEADER = re.compile(
        r'^(error|warning|note|help|info)(\[(?P<code>[^\]]+)\])?:\s*(?P<message>.*)$'
    )

    # Pattern for source location: --> file:line:col
    LOCATION_PATTERN = re.compile(
        r'^\s*-->\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)$'
    )

    # Alternative location pattern: ::: file:line:col (for macro expansions)
    ALT_LOCATION_PATTERN = re.compile(
        r'^\s*:::\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)$'
    )

    # Source code line with line number
    SOURCE_LINE_PATTERN = re.compile(
        r'^\s*(?P<line_num>\d+)\s*\|(?P<code>.*)$'
    )

    # Continuation/annotation line (with | but no line number)
    ANNOTATION_PATTERN = re.compile(
        r'^\s*\|(?P<content>.*)$'
    )

    # For aborting due to errors
    ABORT_PATTERN = re.compile(
        r'^(error|warning): (aborting due to|could not compile|build failed)'
    )

    # Compilation stats
    STATS_PATTERN = re.compile(
        r'^(error|warning): `[^`]+` \(.*\) generated \d+ (error|warning)'
    )

    def __init__(self):
        self.diagnostics = []
        self.raw_lines = []

    def parse_file(self, file_path: str) -> ParsedLog:
        """Parse a cargo log file."""
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return self.parse_string(content, file_path)

    def parse_stream(
        self,
        stream: TextIO,
        source_name: str = "<stdin>",
        on_diagnostic: Optional[Callable[[Diagnostic], None]] = None,
    ) -> ParsedLog:
        """
        Parse cargo log from a stream (e.g., stdin).
        
        Args:
            stream: Input stream to read from
            source_name: Name to use for the source
            on_diagnostic: Optional callback called for each diagnostic as it's parsed
        
        Returns:
            ParsedLog with all parsed diagnostics
        """
        content = stream.read()
        parsed = self.parse_string(content, source_name)
        
        if on_diagnostic:
            for diag in parsed.diagnostics:
                on_diagnostic(diag)
        
        return parsed

    def parse_string(self, content: str, source_name: str = "<string>") -> ParsedLog:
        """Parse cargo log from a string."""
        self.raw_lines = content.splitlines()
        self.diagnostics = []

        i = 0
        while i < len(self.raw_lines):
            line = self.raw_lines[i]

            # Skip abort/stats messages
            if self.ABORT_PATTERN.match(line) or self.STATS_PATTERN.match(line):
                i += 1
                continue

            # Check for diagnostic header
            match = self.DIAGNOSTIC_HEADER.match(line)
            if match:
                diagnostic, end_line = self._parse_diagnostic(i)
                if diagnostic:
                    self.diagnostics.append(diagnostic)
                i = end_line + 1
            else:
                i += 1

        return ParsedLog(
            file_path=source_name,
            diagnostics=self.diagnostics,
            raw_lines=self.raw_lines
        )

    def _parse_diagnostic(self, start_line: int) -> tuple:
        """
        Parse a complete diagnostic block starting at start_line.
        Returns (Diagnostic, end_line_index).
        """
        line = self.raw_lines[start_line]
        match = self.DIAGNOSTIC_HEADER.match(line)
        if not match:
            return None, start_line

        level_str = match.group(1)
        level = DiagnosticLevel(level_str)
        code = match.group('code')
        message = match.group('message').strip()

        diagnostic = Diagnostic(
            level=level,
            message=message,
            code=code,
            line_start=start_line,
        )

        raw_lines = [line]
        context_lines = []
        children = []
        current_line = start_line + 1

        # Parse the body of the diagnostic
        while current_line < len(self.raw_lines):
            line = self.raw_lines[current_line]

            # Check if this is a new top-level diagnostic
            if self.DIAGNOSTIC_HEADER.match(line):
                # But first check if it's a child note/help (indented context)
                if not line.startswith(' ') and not self._is_child_diagnostic(line, diagnostic):
                    break

            # Check for location
            loc_match = self.LOCATION_PATTERN.match(line) or self.ALT_LOCATION_PATTERN.match(line)
            if loc_match:
                if diagnostic.location is None:
                    diagnostic.location = SourceLocation(
                        file_path=loc_match.group('file'),
                        line=int(loc_match.group('line')),
                        column=int(loc_match.group('col'))
                    )
                raw_lines.append(line)
                current_line += 1
                continue

            # Check for source code line
            src_match = self.SOURCE_LINE_PATTERN.match(line)
            if src_match:
                context_lines.append(line)
                raw_lines.append(line)
                current_line += 1
                continue

            # Check for annotation line
            if self.ANNOTATION_PATTERN.match(line):
                context_lines.append(line)
                raw_lines.append(line)
                current_line += 1
                continue

            # Check for child diagnostic (note: or help: within context)
            child_match = self.DIAGNOSTIC_HEADER.match(line.strip())
            if child_match and line.startswith(' '):
                child_level = DiagnosticLevel(child_match.group(1))
                child = Diagnostic(
                    level=child_level,
                    message=child_match.group('message').strip(),
                    code=child_match.group('code'),
                    line_start=current_line,
                    line_end=current_line,
                )
                children.append(child)
                raw_lines.append(line)
                current_line += 1
                continue

            # Check for = note: or = help: style
            eq_match = re.match(r'^\s*=\s*(note|help):\s*(.*)$', line)
            if eq_match:
                child = Diagnostic(
                    level=DiagnosticLevel(eq_match.group(1)),
                    message=eq_match.group(2).strip(),
                    line_start=current_line,
                    line_end=current_line,
                )
                children.append(child)
                raw_lines.append(line)
                current_line += 1
                continue

            # Empty line might be separator or part of message
            if line.strip() == '':
                # Look ahead to see if diagnostic continues
                if current_line + 1 < len(self.raw_lines):
                    next_line = self.raw_lines[current_line + 1]
                    if (self.LOCATION_PATTERN.match(next_line) or
                        self.SOURCE_LINE_PATTERN.match(next_line) or
                        self.ANNOTATION_PATTERN.match(next_line) or
                        next_line.strip().startswith('=')):
                        raw_lines.append(line)
                        current_line += 1
                        continue
                break

            # Other content - might be continuation of message or end
            if line.startswith('   '):  # Indented content
                raw_lines.append(line)
                current_line += 1
                continue

            # Unknown line type - end the diagnostic
            break

        diagnostic.line_end = current_line - 1
        diagnostic.raw_text = '\n'.join(raw_lines)
        diagnostic.children = children
        diagnostic.context_lines = context_lines

        return diagnostic, current_line - 1

    def _is_child_diagnostic(self, line: str, parent: Diagnostic) -> bool:
        """Check if a diagnostic line is a child of the parent."""
        # Standalone note/help that follows immediately might be related
        if line.startswith('note:') or line.startswith('help:'):
            return True
        return False


class LogQuery:
    """
    High-level query interface for cargo logs.
    Designed for LLM consumption with clear, structured responses.
    """

    def __init__(self, parsed_log: ParsedLog):
        self.log = parsed_log

    def find_errors(
        self,
        file_pattern: Optional[str] = None,
        message_pattern: Optional[str] = None,
        code_pattern: Optional[str] = None,
    ) -> list:
        """
        Find all errors matching the given criteria.
        
        Args:
            file_pattern: Regex to match file paths (e.g., "tests/.*" for test files)
            message_pattern: Regex to match error messages (e.g., "not found in")
            code_pattern: Regex to match error codes (e.g., "E0425" or "E04.*")
        
        Returns:
            List of matching Diagnostic objects
        """
        return self.log.filter(
            level=DiagnosticLevel.ERROR,
            file_pattern=file_pattern,
            message_pattern=message_pattern,
            code_pattern=code_pattern,
        )

    def find_warnings(
        self,
        file_pattern: Optional[str] = None,
        message_pattern: Optional[str] = None,
        code_pattern: Optional[str] = None,
    ) -> list:
        """Find all warnings matching the given criteria."""
        return self.log.filter(
            level=DiagnosticLevel.WARNING,
            file_pattern=file_pattern,
            message_pattern=message_pattern,
            code_pattern=code_pattern,
        )

    def find_by_file(self, file_pattern: str) -> list:
        """
        Find all diagnostics for files matching the pattern.
        
        Args:
            file_pattern: Regex pattern for file paths
                         Examples: "src/main.rs", "tests/.*", ".*/mod.rs"
        """
        return self.log.filter(file_pattern=file_pattern)

    def find_by_message(self, message_pattern: str) -> list:
        """
        Find all diagnostics with messages matching the pattern.
        
        Args:
            message_pattern: Regex pattern for messages
                            Examples: "not found", "unused.*variable", "lifetime"
        """
        return self.log.filter(message_pattern=message_pattern)

    def find_by_code(self, code_pattern: str) -> list:
        """
        Find all diagnostics with matching error/warning codes.
        
        Args:
            code_pattern: Regex for codes (e.g., "E0425", "E04.*", "unused_.*")
        """
        return self.log.filter(code_pattern=code_pattern)

    def get_error_boundaries(self, diagnostic: Diagnostic) -> dict:
        """
        Get the exact boundaries of an error in the log file.
        
        Returns:
            {
                "line_start": int,  # Starting line in log file (0-indexed)
                "line_end": int,    # Ending line in log file (0-indexed)
                "raw_text": str,    # The complete raw error block
            }
        """
        return {
            "line_start": diagnostic.line_start,
            "line_end": diagnostic.line_end,
            "raw_text": diagnostic.raw_text,
        }

    def get_unique_error_codes(self) -> list:
        """Get all unique error codes in the log."""
        codes = set()
        for d in self.log.diagnostics:
            if d.code:
                codes.add(d.code)
        return sorted(codes)

    def get_affected_files(self, level: Optional[DiagnosticLevel] = None) -> list:
        """Get all files that have diagnostics."""
        diagnostics = self.log.diagnostics
        if level:
            diagnostics = [d for d in diagnostics if d.level == level]
        
        files = set()
        for d in diagnostics:
            if d.location:
                files.add(d.location.file_path)
        return sorted(files)

    def group_by_file(
        self,
        level: Optional[DiagnosticLevel] = None,
        file_pattern: Optional[str] = None,
    ) -> dict:
        """
        Group diagnostics by file path.
        
        Returns:
            {
                "src/main.rs": [Diagnostic, ...],
                "src/lib.rs": [Diagnostic, ...],
            }
        """
        diagnostics = self.log.diagnostics
        if level:
            diagnostics = [d for d in diagnostics if d.level == level]
        if file_pattern:
            diagnostics = [d for d in diagnostics if d.matches_file_regex(file_pattern)]

        grouped = {}
        for d in diagnostics:
            if d.location:
                file_path = d.location.file_path
                if file_path not in grouped:
                    grouped[file_path] = []
                grouped[file_path].append(d)
        return grouped

    def group_by_code(
        self,
        level: Optional[DiagnosticLevel] = None,
    ) -> dict:
        """
        Group diagnostics by error/warning code.
        
        Returns:
            {
                "E0425": [Diagnostic, ...],
                "E0308": [Diagnostic, ...],
            }
        """
        diagnostics = self.log.diagnostics
        if level:
            diagnostics = [d for d in diagnostics if d.level == level]

        grouped = {}
        for d in diagnostics:
            code = d.code or "no_code"
            if code not in grouped:
                grouped[code] = []
            grouped[code].append(d)
        return grouped

    def to_json(
        self,
        diagnostics: Optional[list] = None,
        include_raw: bool = True,
        indent: int = 2,
    ) -> str:
        """
        Convert diagnostics to JSON for LLM consumption.
        
        Args:
            diagnostics: List of diagnostics (defaults to all)
            include_raw: Include raw log text in output
            indent: JSON indentation
        """
        if diagnostics is None:
            diagnostics = self.log.diagnostics

        output = {
            "summary": self.log.summary(),
            "diagnostics": []
        }

        for d in diagnostics:
            entry = d.to_dict()
            if not include_raw:
                del entry["raw_text"]
            output["diagnostics"].append(entry)

        return json.dumps(output, indent=indent)

    def format_for_llm(
        self,
        diagnostics: Optional[list] = None,
        verbose: bool = False,
    ) -> str:
        """
        Format diagnostics in a readable format optimized for LLM analysis.
        
        Args:
            diagnostics: List of diagnostics (defaults to all)
            verbose: Include full raw text blocks
        """
        if diagnostics is None:
            diagnostics = self.log.diagnostics

        if not diagnostics:
            return "No diagnostics found matching the criteria."

        lines = []
        lines.append(f"Found {len(diagnostics)} diagnostic(s):\n")

        for i, d in enumerate(diagnostics, 1):
            lines.append(f"{'='*60}")
            lines.append(f"[{i}] {d.level.value.upper()}", )
            if d.code:
                lines.append(f"    Code: {d.code}")
            lines.append(f"    Message: {d.message}")
            if d.location:
                lines.append(f"    Location: {d.location}")
            lines.append(f"    Log lines: {d.line_start}-{d.line_end}")

            if d.children:
                for child in d.children:
                    lines.append(f"    └─ {child.level.value}: {child.message}")

            if verbose and d.raw_text:
                lines.append("\n    Raw output:")
                for raw_line in d.raw_text.split('\n'):
                    lines.append(f"    │ {raw_line}")

            lines.append("")

        return '\n'.join(lines)


def main():
    """CLI interface for the cargo log parser."""
    parser = argparse.ArgumentParser(
        description="Parse cargo build logs and filter errors/warnings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show all errors
  %(prog)s build.log --errors

  # Pipe from cargo build
  cargo build 2>&1 | %(prog)s --errors
  cargo build 2>&1 | %(prog)s --file "tests/.*"

  # Use - for stdin explicitly
  cargo build 2>&1 | %(prog)s - --errors --file "src/.*"

  # Find errors in test files
  %(prog)s build.log --errors --file "tests/.*"

  # Find "not found" errors
  %(prog)s build.log --errors --message "not found"

  # Find specific error code
  %(prog)s build.log --code "E0425"

  # Output as JSON
  %(prog)s build.log --json

  # Group by file
  %(prog)s build.log --group-by-file
  
  # Stream mode - output each match immediately (useful with stdin)
  cargo build 2>&1 | %(prog)s --errors --stream
        """
    )

    parser.add_argument(
        "log_file", 
        nargs="?",
        default="-",
        help="Path to cargo build log file (use - or omit for stdin)"
    )
    parser.add_argument("--errors", "-e", action="store_true", help="Show only errors")
    parser.add_argument("--warnings", "-w", action="store_true", help="Show only warnings")
    parser.add_argument("--file", "-f", metavar="PATTERN", help="Filter by file path regex")
    parser.add_argument("--message", "-m", metavar="PATTERN", help="Filter by message regex")
    parser.add_argument("--code", "-c", metavar="PATTERN", help="Filter by error code regex")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Include raw log text")
    parser.add_argument("--summary", "-s", action="store_true", help="Show summary only")
    parser.add_argument("--group-by-file", action="store_true", help="Group diagnostics by file")
    parser.add_argument("--group-by-code", action="store_true", help="Group diagnostics by error code")
    parser.add_argument("--list-codes", action="store_true", help="List all unique error codes")
    parser.add_argument("--list-files", action="store_true", help="List all affected files")
    parser.add_argument(
        "--stream", 
        action="store_true", 
        help="Stream mode: output each matching diagnostic immediately"
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Output raw log text only (useful for piping)"
    )
    parser.add_argument(
        "--passthrough",
        action="store_true",
        help="Pass through all input while also outputting matches"
    )

    args = parser.parse_args()

    # Determine level filter
    level = None
    if args.errors:
        level = DiagnosticLevel.ERROR
    elif args.warnings:
        level = DiagnosticLevel.WARNING

    # Create filter function
    def matches_filter(diag: Diagnostic) -> bool:
        if level is not None and diag.level != level:
            return False
        if args.file and not diag.matches_file_regex(args.file):
            return False
        if args.message and not diag.matches_message_regex(args.message):
            return False
        if args.code and not diag.matches_code(args.code):
            return False
        return True

    # Parse input (file or stdin)
    parser_instance = CargoLogParser()
    
    if args.log_file == "-" or (args.log_file == "-" and not sys.stdin.isatty()):
        # Reading from stdin
        if sys.stdin.isatty() and args.log_file == "-":
            print("Reading from stdin... (Ctrl+D to end, or pipe input)", file=sys.stderr)
        
        if args.passthrough:
            # Read all input, print it, then parse
            content = sys.stdin.read()
            print(content, end='')
            parsed = parser_instance.parse_string(content, "<stdin>")
        else:
            parsed = parser_instance.parse_stream(sys.stdin, "<stdin>")
    else:
        # Reading from file
        parsed = parser_instance.parse_file(args.log_file)

    query = LogQuery(parsed)

    # Handle special list commands
    if args.list_codes:
        codes = query.get_unique_error_codes()
        if args.json:
            print(json.dumps(codes))
        else:
            print("Unique error/warning codes:")
            for code in codes:
                print(f"  {code}")
        return

    if args.list_files:
        files = query.get_affected_files(level)
        if args.json:
            print(json.dumps(files))
        else:
            print("Affected files:")
            for f in files:
                print(f"  {f}")
        return

    if args.summary:
        summary = parsed.summary()
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(f"Log file: {summary['file']}")
            print(f"Total diagnostics: {summary['total_diagnostics']}")
            print(f"  Errors: {summary['errors']}")
            print(f"  Warnings: {summary['warnings']}")
            print(f"  Notes: {summary['notes']}")
            if summary['error_codes']:
                print(f"Error codes: {', '.join(summary['error_codes'])}")
            if summary['affected_files']:
                print(f"Affected files: {len(summary['affected_files'])}")
        return

    # Filter diagnostics
    diagnostics = [d for d in parsed.diagnostics if matches_filter(d)]

    # Handle grouping
    if args.group_by_file:
        grouped = {}
        for d in diagnostics:
            if d.location:
                fp = d.location.file_path
                if fp not in grouped:
                    grouped[fp] = []
                grouped[fp].append(d)
        
        if args.json:
            output = {f: [d.to_dict() for d in diags] for f, diags in grouped.items()}
            print(json.dumps(output, indent=2))
        else:
            for file_path, diags in grouped.items():
                print(f"\n{file_path} ({len(diags)} issues):")
                for d in diags:
                    print(f"  - {d.summary()}")
        return

    if args.group_by_code:
        grouped = {}
        for d in diagnostics:
            code = d.code or "no_code"
            if code not in grouped:
                grouped[code] = []
            grouped[code].append(d)
        
        if args.json:
            output = {c: [d.to_dict() for d in diags] for c, diags in grouped.items()}
            print(json.dumps(output, indent=2))
        else:
            for code, diags in grouped.items():
                print(f"\n{code} ({len(diags)} occurrences):")
                for d in diags:
                    loc = f" at {d.location}" if d.location else ""
                    print(f"  - {d.message}{loc}")
        return

    # Output results
    if args.raw:
        # Output only raw log text for matching diagnostics
        for d in diagnostics:
            print(d.raw_text)
            print()  # Blank line between
    elif args.stream or args.json:
        if args.json:
            print(query.to_json(diagnostics, include_raw=args.verbose))
        else:
            # Stream mode - one diagnostic at a time
            for d in diagnostics:
                if args.verbose:
                    print(d.raw_text)
                else:
                    print(d.summary())
    else:
        print(query.format_for_llm(diagnostics, verbose=args.verbose))


if __name__ == "__main__":
    main()
