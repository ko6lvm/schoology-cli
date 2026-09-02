# schoology-cli
## Session Management
Once you run anything that fetches data from Schoology, it will open a browser for you to log into schoology.<br>
Login data will be retained in `.browser_profile`, which is not uploaded to GitHub (see `.gitignore`)

## Architecture: How Data is Fetched

### Grades (`grades.py`)
- **Single-Page Fetch**: Schoology serves the entire gradebook report for all enrolled courses on a single page (`/grades/grades`).
- **No Multithreading Needed**: Because all courses and grade items are loaded in one single request, grades retrieval does not require parallel requests and cannot trigger HTTP 429 rate limits.

### Assignments (`assignments.py`)
- **Materials + Parallel Details Fetch**: The course materials page (`/course/{id}/materials?list_filter=assignments`) provides the assignment list. To extract full instructions, exact due dates, file attachments, and submission history, each assignment's info page (`/assignment/{id}/info`) must be retrieved.
- **Multithreaded HTTP Worker Pool**: Uses session cookies extracted from Playwright to fetch assignment details concurrently.
- **Rate-Limit Safe (HTTP 429 Prevention)**: Defaults to **2 concurrent workers** with an automatic exponential backoff retry mechanism (0.35s base delay) if Schoology's burst detector throttles a request.

---

## Datatypes
### grades.py
```python
[
  {
    "id": str,                  # e.g., "1234" (Course ID)
    "title": str,               # e.g., "Algebra 2/Trig - 1234: TeacherName p5 T1"
    "grade": Optional[str],     # e.g., "A+ ( 99.07% )" or "100%" or None
    "periods": [
      {
        "id": str,              # e.g., "1159172" (Grading Period ID)
        "title": str,           # e.g., "26-27 T1"
        "weight": Optional[str],# e.g., "100%" or None
        "grade": Optional[str], # e.g., "A+ ( 99.07% )" or None
        "categories": [
          {
            "id": str,          # e.g., "1159172-94406961" (Category ID)
            "title": str,       # e.g., "Assignments"
            "weight": Optional[str], # e.g., "15%" or None
            "grade": Optional[str],  # e.g., "A+ ( 99.07% )" or None
            "items": [
              {
                "id": str,          # e.g., "I-123412341234" (Assignment Item ID)
                "title": str,       # e.g., "A2T - CW/HW1 - A2T Math Survey"
                "grade": Optional[str], # e.g., "10 / 10" or None (if ungraded)
                "due_date": Optional[str], # e.g., "8/19/26" or "8/21/26 11:59pm" or None
                "comment": Optional[str],  # e.g., "turned in 8/27" or None
                "url": Optional[str]       # e.g., "/assignment/123412341234" or None
              }
            ]
          }
        ]
      }
    ]
  }
]
```

### assignments.py
```python
{
  "course_id": str,               # e.g., "8465379643"
  "course_title": str,            # e.g., "Comp Prog Java - 2370"
  "course_url": str,              # e.g., "https://<school>.schoology.com/course/8465379643/materials"
  "total_assignments": int,       # e.g., 4
  "assignments": [
    {
      "id": str,                  # e.g., "8495011888" (Assignment ID)
      "title": str,               # e.g., "Java: Lab_HardwareMap"
      "url": str,                 # e.g., "https://<school>.schoology.com/assignment/8495011888"
      "category": Optional[str],  # e.g., "Assignment" or "Tests & Quizzes"
      "folder": Optional[str],    # e.g., "Lab_HardwareMap" (Parent Folder)
      "description_snippet": Optional[str],
      "due_date": Optional[str],  # e.g., "Thursday, August 27, 2026 at 11:59 pm"
      "posted_date": Optional[str], # e.g., "Thu Aug 13, 2026 at 1:17 pm"
      "grading_period": Optional[str], # e.g., "26-27 T1"
      "description": Optional[str],    # Full plain text instructions
      "description_html": Optional[str], # Full HTML instructions
      "attachments": [
        {
          "title": str,           # e.g., "Lab_Hardware_Map_Remote_Learning_v01 - sample.pdf"
          "url": Optional[str],   # Direct file download link
          "viewer_url": Optional[str], # In-browser docviewer link
          "size": Optional[str]   # e.g., "808 KB"
        }
      ],
      "submissions": [
        {
          "submitted_at": str,    # e.g., "Aug 27, 2026 at 9:40 pm"
          "status": Optional[str], # e.g., "On time" or "Late"
          "summary": Optional[str], # e.g., "1 item · On time"
          "url": Optional[str]    # Direct dropbox revision link
        }
      ],
      "comments_count": int       # e.g., 0
    }
  ]
}
```

## CLI Usage

### Interactive CLI (`main.py`)
```bash
# Launch interactive dual-mode picker (Grades or Assignments)
python main.py

# Launch directly into Grades mode
python main.py --grades

# Launch directly into Assignments mode
python main.py --assignments

# Parallel options for assignment fetching (default: 2 workers)
python main.py --workers 2
python main.py --no-parallel   # sequential fallback

# Live streaming status logs on stderr (default: enabled)
python main.py --logs
python main.py --no-logs       # or --quiet / -q
```

### Direct Grades Fetcher (`grades.py`)
```bash
# Fetch and output gradebook JSON
python grades.py --json

# Run silently without stderr logs
python grades.py --json --quiet
```

### Fetch Course Assignments (`assignments.py`)
```bash
# Interactive selection: lists enrolled courses to choose from
python assignments.py

# Query by course title or keyword
python assignments.py --course "Java"

# Query by course ID
python assignments.py --course 8465379643

# Output raw JSON to stdout
python assignments.py --course "Java" --json

# Fast mode: fetch assignment list without loading individual assignment detail pages
python assignments.py --course "Biology" --fast

# Parallel fetching with custom concurrency (default: 2 workers)
python assignments.py --course "Algebra" --workers 2
python assignments.py --course "Algebra" --no-parallel  # sequential fallback

# Fetch details for a single assignment by ID or URL
python assignments.py --assignment 8495011888

# List all courses
python assignments.py --list-courses
```