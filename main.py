import sys
import argparse
import webbrowser
from typing import List, Dict, Any, Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from grades import fetch_grades
from assignments import fetch_courses, fetch_course_assignments, fetch_assignment_details

console = Console()

# Configuration state
config = {
    "show_logs": True,
    "headless": True,
    "parallel": True,
    "workers": 2,
}

# Cache session data so switching modes does not re-fetch unnecessarily
cached_grades_courses: Optional[List[Dict[str, Any]]] = None
cached_assignments_courses: Optional[List[Dict[str, Any]]] = None


def print_header():
    console.clear()
    console.print("")
    console.print("[bold blue]Schoology CLI v0.1[/bold blue] [bold green]ko6lvm/schoology-cli[/bold green]")
    console.print("")


def prompt_mode() -> str:
    """
    Prompts the user to choose between retrieving Grades or Assignments.
    """
    while True:
        print_header()
        console.print("[bold green]What would you like to retrieve?[/bold green]")
        console.print("  (1) [bold yellow]Grades[/bold yellow] (Course grades report)")
        console.print("  (2) [bold cyan]Assignments[/bold cyan] (Materials, due dates, instructions)")
        console.print("")
        console.print("[bold green]Selection (1/2, or 'q' to quit):[/bold green] ", end="")

        try:
            choice = input().strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold yellow]Goodbye![/bold yellow]")
            sys.exit(0)

        if choice in ("q", "quit", "exit"):
            console.print("[bold yellow]Goodbye![/bold yellow]")
            sys.exit(0)
        elif choice in ("1", "g", "grades", "grade"):
            return "grades"
        elif choice in ("2", "a", "assignments", "assignment"):
            return "assignments"
        else:
            console.print("\n[bold red]Invalid option. Please enter 1 or 2.[/bold red]")
            input("Press Enter to try again...")


def get_grades_courses() -> List[Dict[str, Any]]:
    """
    Retrieves and caches courses with gradebook data.
    """
    global cached_grades_courses
    if cached_grades_courses is None:
        print_header()
        console.print("[bold green]Fetching grades (this might take a few seconds)...[/bold green]")
        if config["show_logs"]:
            console.print("[dim]Streaming live status logs from browser session:[/dim]\n")
        cached_grades_courses = fetch_grades(
            headless=config["headless"],
            print_logs=config["show_logs"],
        )
    return cached_grades_courses


def get_assignments_courses() -> List[Dict[str, Any]]:
    """
    Retrieves and caches enrolled courses for assignments.
    """
    global cached_assignments_courses
    if cached_assignments_courses is None:
        print_header()
        console.print("[bold green]Fetching course list (this might take a few seconds)...[/bold green]")
        if config["show_logs"]:
            console.print("[dim]Streaming live status logs from browser session:[/dim]\n")
        cached_assignments_courses = fetch_courses(
            headless=config["headless"],
            print_logs=config["show_logs"],
        )
    return cached_assignments_courses


def display_course_grades(course: Dict[str, Any]):
    """
    Renders detailed grades breakdown for a selected course.
    """
    print_header()
    course_grade = course.get("grade") or "No grade"
    console.print(f"[bold blue]{course['title']}[/bold blue] [bold yellow]{course_grade}[/bold yellow]\n")

    has_items = False
    for period in course.get("periods", []):
        for category in period.get("categories", []):
            for item in category.get("items", []):
                has_items = True
                due_info = f" (Due: {item['due_date']})" if item.get("due_date") else ""
                cat_title = category.get("title", "Category")
                item_title = item.get("title", "Untitled")

                if item.get("grade") is None:
                    console.print(f"  [bold blue][{cat_title}][/bold blue] {item_title}: [bold red]Ungraded[/bold red]{due_info}")
                    continue

                raw_grade = item["grade"]
                parts = raw_grade.split()

                try:
                    g_idx = 1 if len(parts) == 4 else 0
                    o_idx = 3 if len(parts) == 4 else 2
                    grade = float(parts[g_idx])
                    grade_out_of = float(parts[o_idx])

                    if grade_out_of > 0:
                        pct = (grade / grade_out_of) * 100
                        g_str = f"{int(grade)}" if grade.is_integer() else f"{grade}"
                        o_str = f"{int(grade_out_of)}" if grade_out_of.is_integer() else f"{grade_out_of}"
                        console.print(f"  [bold blue][{cat_title}][/bold blue] {item_title}: {g_str}/{o_str} {pct:.1f}%{due_info}")
                    else:
                        g_str = f"{int(grade)}" if grade.is_integer() else f"{grade}"
                        console.print(f"  [bold blue][{cat_title}][/bold blue] {item_title}: {g_str}/{parts[o_idx]}{due_info}")
                except (ValueError, IndexError):
                    console.print(f"  [bold blue][{cat_title}][/bold blue] {item_title}: {raw_grade}{due_info}")

    if not has_items:
        console.print("  [yellow]No assignments found in this gradebook.[/yellow]")

    console.print("")
    try:
        input("Hit enter to return...")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[bold yellow]Goodbye![/bold yellow]")
        sys.exit(0)


def display_course_assignments(course: Dict[str, Any]):
    """
    Fetches and renders assignments for a selected course with optional item inspection.
    """
    print_header()
    course_title = course.get("title", "Course")
    console.print(f"[bold green]Fetching assignments for {course_title}...[/bold green]")
    if config["show_logs"]:
        console.print("[dim]Streaming live status logs from browser session:[/dim]\n")

    course_id = course.get("id")
    data = fetch_course_assignments(
        course=course_id,
        fetch_details=True,
        parallel=config["parallel"],
        max_workers=config["workers"],
        headless=config["headless"],
        print_logs=config["show_logs"],
    )
    assignments = data.get("assignments", [])

    while True:
        print_header()
        console.print(Panel.fit(
            f"[bold blue]{course_title}[/bold blue] (ID: {course_id})\n"
            f"[green]Assignments Found: {len(assignments)}[/green]",
            title="Schoology Assignments",
            border_style="blue",
        ))

        if not assignments:
            console.print("[yellow]No assignments found for this course.[/yellow]\n")
            input("Hit enter to return...")
            return

        for i, asgn in enumerate(assignments, 1):
            category = asgn.get("category") or "Assignment"
            title = asgn.get("title") or "Untitled"
            due = asgn.get("due_date")
            due_info = f" (Due: {due})" if due else ""

            subs = asgn.get("submissions", [])
            if subs:
                status_parts = [f"{s.get('status', 'Submitted')} ({s.get('submitted_at', '')})".strip() for s in subs]
                status_str = f" [bold green][{', '.join(status_parts)}][/bold green]"
            else:
                status_str = ""

            atts = asgn.get("attachments", [])
            att_info = f" [dim]({len(atts)} attachment{'s' if len(atts) > 1 else ''})[/dim]" if atts else ""

            padding = " " * (len(str(len(assignments))) - len(str(i)))
            console.print(f"  ({i}){padding} [bold blue][{category}][/bold blue] {title}{due_info}{status_str}{att_info}")

        console.print("")
        console.print("[bold green]Enter assignment # for details (or hit Enter to return):[/bold green] ", end="")

        try:
            choice = input().strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold yellow]Goodbye![/bold yellow]")
            sys.exit(0)

        if not choice:
            return

        if choice.isdigit() and 1 <= int(choice) <= len(assignments):
            item = assignments[int(choice) - 1]
            while True:
                print_header()
                attachments = item.get("attachments", [])
                submissions = item.get("submissions", [])

                openable_links = []  # list of tuples: (label, url)

                if attachments:
                    att_lines = []
                    for idx, a in enumerate(attachments, 1):
                        open_url = a.get("viewer_url") or a.get("url")
                        key_prefix = f"a{idx}"
                        if open_url:
                            openable_links.append((key_prefix, a.get("title", f"Attachment {idx}"), open_url))
                            att_lines.append(f"  [{key_prefix}] [bold cyan]{a['title']}[/bold cyan] ({a.get('size') or 'file'})")
                        else:
                            att_lines.append(f"  • {a['title']} ({a.get('size') or 'file'})")
                    atts_desc = "\n".join(att_lines)
                else:
                    atts_desc = "  None"

                if submissions:
                    sub_lines = []
                    for idx, s in enumerate(submissions, 1):
                        open_url = s.get("url")
                        key_prefix = f"s{idx}"
                        summary_str = s.get("summary") or s.get("status") or "Submission"
                        submitted_str = f" on {s.get('submitted_at')}" if s.get("submitted_at") else ""
                        if open_url:
                            openable_links.append((key_prefix, f"Submission revision {idx}", open_url))
                            sub_lines.append(f"  [{key_prefix}] [bold green]{summary_str}[/bold green]{submitted_str}")
                        else:
                            sub_lines.append(f"  • {summary_str}{submitted_str}")
                    subs_desc = "\n".join(sub_lines)
                else:
                    subs_desc = "  None"

                console.print(Panel.fit(
                    f"[bold blue]{item.get('title')}[/bold blue] (ID: {item.get('id')})\n"
                    f"[yellow]Due Date:[/yellow] {item.get('due_date') or 'No due date'}\n"
                    f"[magenta]Category:[/magenta] {item.get('category') or 'Uncategorized'}\n"
                    f"[cyan]Grading Period:[/cyan] {item.get('grading_period') or 'N/A'}\n"
                    f"[white]Posted Date:[/white] {item.get('posted_date') or 'N/A'}\n\n"
                    f"[bold]Description / Instructions:[/bold]\n{item.get('description') or 'No description provided.'}\n\n"
                    f"[bold]Attachments (Received):[/bold]\n{atts_desc}\n\n"
                    f"[bold]Submissions:[/bold]\n{subs_desc}",
                    title=f"Assignment #{choice} Details",
                    border_style="cyan",
                ))
                console.print("")

                if openable_links:
                    options_display = ", ".join([f"'{k}' for {lbl}" for k, lbl, _ in openable_links])
                    console.print(f"[bold green]Open link in browser ({options_display}), or hit Enter to return:[/bold green] ", end="")
                else:
                    console.print("[bold green]Hit Enter to return:[/bold green] ", end="")

                try:
                    action = input().strip().lower()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[bold yellow]Goodbye![/bold yellow]")
                    sys.exit(0)

                if not action:
                    break

                target_link = next((url for k, _, url in openable_links if k.lower() == action), None)
                if target_link:
                    console.print(f"[bold green]Opening in browser: {target_link}[/bold green]")
                    webbrowser.open(target_link)
                    time_to_wait = 1
                else:
                    console.print(f"[bold red]Unknown option '{action}'.[/bold red]")
                    input("Press Enter to continue...")
        else:
            console.print(f"[bold red]Invalid assignment number '{choice}'.[/bold red]")
            input("Press Enter to continue...")


def run_course_picker(mode: str) -> Optional[str]:
    """
    Renders the active 0-to-last course picker for the chosen mode.
    Returns "switch_mode" if user requests mode change, or None when looping.
    """
    if mode == "grades":
        courses = get_grades_courses()
    else:
        courses = get_assignments_courses()

    print_header()
    mode_label = "[bold yellow]Grades[/bold yellow]" if mode == "grades" else "[bold cyan]Assignments[/bold cyan]"
    console.print(f"Viewing: {mode_label} Mode\n")

    for i, course in enumerate(courses):
        padding = " " * (len(str(len(courses) - 1)) - len(str(i)))
        if mode == "grades":
            grade_display = course.get("grade") or "No grade"
            console.print(f"({i}){padding} [bold blue]{course['title']}[/bold blue] [bold yellow]{grade_display}[/bold yellow]")
        else:
            sec = f" [dim]({course.get('section', '')})[/dim]" if course.get("section") else ""
            console.print(f"({i}){padding} [bold blue]{course['title']}[/bold blue]{sec}")

    console.print("")
    console.print(f"[bold green]Get {mode} for course (0-{len(courses) - 1}, 'm' for mode, 'q' to quit):[/bold green] ", end="")

    try:
        raw_input = input().strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[bold yellow]Goodbye![/bold yellow]")
        sys.exit(0)

    if raw_input in ("q", "quit", "exit"):
        console.print("[bold yellow]Goodbye![/bold yellow]")
        sys.exit(0)

    if raw_input in ("m", "mode", "switch", "change"):
        return "switch_mode"

    if not raw_input.isdigit() or int(raw_input) >= len(courses) or int(raw_input) < 0:
        console.print(f"[bold red]Invalid selection '{raw_input}'. Please enter a number between 0 and {len(courses) - 1}.[/bold red]")
        input("Press Enter to continue...")
        return None

    selected_course = courses[int(raw_input)]

    if mode == "grades":
        display_course_grades(selected_course)
    else:
        display_course_assignments(selected_course)

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Schoology CLI: view grades and assignments interactively."
    )
    parser.add_argument(
        "--grades",
        "-g",
        action="store_true",
        help="Launch directly into Grades mode",
    )
    parser.add_argument(
        "--assignments",
        "-a",
        action="store_true",
        help="Launch directly into Assignments mode",
    )
    parser.add_argument(
        "--print-logs",
        "--logs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Toggle live status logs on stderr (default: True, use --no-logs to disable)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_false",
        dest="print_logs",
        help="Suppress live status logs on stderr (alias for --no-logs)",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=2,
        help="Number of concurrent workers for parallel fetching (default: 2)",
    )
    parser.add_argument(
        "--parallel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use parallel HTTP worker pool for fetching assignment details (default: True, use --no-parallel for sequential)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser in headless mode (default: True)",
    )

    args = parser.parse_args()
    config["show_logs"] = args.print_logs
    config["headless"] = args.headless
    config["parallel"] = args.parallel
    config["workers"] = args.workers

    initial_mode = None
    if args.grades:
        initial_mode = "grades"
    elif args.assignments:
        initial_mode = "assignments"

    mode = initial_mode or prompt_mode()

    while True:
        action = run_course_picker(mode)
        if action == "switch_mode":
            mode = "assignments" if mode == "grades" else "grades"


if __name__ == "__main__":
    main()