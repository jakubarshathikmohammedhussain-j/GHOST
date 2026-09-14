import os
import json
import time
import re
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# -------------------------------------------------------------
# PROFILE HEURISTICS MATRIX (Mohammed Hussain J.)
# Background: MBA (Analytics & Supply Chain), B.Com
# Core: Operations Architecture, Tech Strategy, LLM/AI Workflows, BI
# -------------------------------------------------------------
PROFILE_KEYWORDS = {
    "tier_1_core": [
        "consulting", "strategy", "operations", "business intelligence", 
        "supply chain", "product manager", "management consultant",
        "digital transformation", "business analyst", "automation"
    ],
    "tier_2_skills": [
        "python", "data analytics", "logistics", "ai", "llm", "pipeline", 
        "process optimization", "scrm", "workflow", "tableau", "power bi"
    ],
    "experience_filter": [
        "junior", "entry", "associate", "intern", "internship", "analyst", 
        "graduate", "trainee", "0-2", "0-3", "1-3", "early career"
    ]
}

TARGET_LOCATIONS = ["remote", "chennai", "india", "dubai", "uae", "singapore", "europe", "germany", "uk", "netherlands"]

COLUMNS = [
    "Date Found", "Job Title", "Company", "Location", "Compensation",
    "Portfolio Match (%)", "Strategic Match Rationale", "Visa Status",
    "Application Link", "Hiring Lead Search Query"
]

def calculate_portfolio_match(title, description):
    text = f"{title} {description}".lower()
    
    score = 40  # Baseline for matching search queries
    rationale = []

    # Check experience tier
    exp_matched = [word for word in PROFILE_KEYWORDS["experience_filter"] if word in text]
    if exp_matched or ("senior" not in title.lower() and "lead" not in title.lower() and "director" not in title.lower()):
        score += 20
        rationale.append("Aligned with 0-3Y / Early-Career Scope")
    else:
        score -= 20

    # Match primary strategy/consulting domains
    core_hits = [k for k in PROFILE_KEYWORDS["tier_1_core"] if k in text]
    if core_hits:
        score += min(len(core_hits) * 8, 25)
        rationale.append(f"Domain Focus: {', '.join(core_hits[:3]).title()}")

    # Match technical/data architecture capabilities
    tech_hits = [k for k in PROFILE_KEYWORDS["tier_2_skills"] if k in text]
    if tech_hits:
        score += min(len(tech_hits) * 5, 15)
        rationale.append(f"Tech Alignment: {', '.join(tech_hits[:3]).upper()}")

    score = max(30, min(score, 98))
    rationale_str = " | ".join(rationale) if rationale else "General operations profile relevance"
    return score, rationale_str

def detect_visa_status(text):
    text_lower = text.lower()
    if any(k in text_lower for k in ["visa sponsorship", "visa supported", "sponsorship available", "work permit provided"]):
        return "Sponsorship Offered"
    elif any(k in text_lower for k in ["no sponsorship", "must have valid work authorization", "citizens only", "no visa"]):
        return "No Sponsorship"
    return "Undisclosed / Verify"

def fetch_job_stream():
    jobs = []
    
    # Source 1: Arbeitnow Global API (Comprehensive Visa Sponsorship & European Data)
    try:
        res = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=15)
        if res.status_code == 200:
            data = res.json().get("data", [])
            for item in data:
                title = item.get("title", "")
                desc = item.get("description", "")
                location = item.get("location", "")
                remote = item.get("remote", False)
                sponsored = item.get("visa_sponsorship", False)
                url = item.get("url", "")
                company = item.get("company_name", "")

                text_combined = f"{title} {desc} {location}".lower()

                # Filter roles
                is_target_role = any(r in text_combined for r in ["consult", "strateg", "operat", "product", "analyst", "intern"])
                is_target_geo = any(loc in text_combined for loc in TARGET_LOCATIONS) or remote

                if is_target_role and is_target_geo:
                    visa_status = "Sponsorship Offered" if sponsored else detect_visa_status(desc)
                    is_intern = "intern" in title.lower() or "internship" in title.lower() or "intern" in desc.lower()[:300]
                    
                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": "Remote" if remote else location,
                        "salary": "Disclosed in Portal" if "€" not in desc and "$" not in desc else "Competitive / Stated",
                        "description": desc[:350] + "...",
                        "url": url,
                        "visa": visa_status,
                        "is_intern": is_intern,
                        "is_remote": remote
                    })
    except Exception as e:
        print(f"[GHOST] Source 1 fetch error: {e}")

    # Source 2: Remotive Global Strategy & Product API
    try:
        res = requests.get("https://remotive.com/api/remote-jobs?limit=50", timeout=15)
        if res.status_code == 200:
            remotive_jobs = res.json().get("jobs", [])
            for item in remotive_jobs:
                title = item.get("title", "")
                category = item.get("category", "").lower()
                desc = item.get("description", "")
                company = item.get("company_name", "")
                url = item.get("url", "")
                salary = item.get("salary", "Disclosed in Portal")

                if any(c in category for c in ["product", "business", "data", "finance"]):
                    is_intern = "intern" in title.lower() or "intern" in desc.lower()[:200]
                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": "Global / Remote",
                        "salary": salary if salary else "Disclosed in Portal",
                        "description": desc[:350] + "...",
                        "url": url,
                        "visa": detect_visa_status(desc),
                        "is_intern": is_intern,
                        "is_remote": True
                    })
    except Exception as e:
        print(f"[GHOST] Source 2 fetch error: {e}")

    return jobs

def send_recon_email(dispatched_jobs, recipient_email):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_APP_PASSWORD')
    
    if not sender_email or not sender_password or not dispatched_jobs:
        return

    # Take top 5 highest matching roles
    top_matches = sorted(dispatched_jobs, key=lambda x: x['match_score'], reverse=True)[:5]
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    msg = MIMEMultipart()
    msg['From'] = f"GHOST Recon Node <{sender_email}>"
    msg['To'] = recipient_email
    msg['Subject'] = f"GHOST Alert: {len(dispatched_jobs)} Strategic Positions Logged ({today_str})"

    def generate_job_card(j):
        encoded_query = urllib.parse.quote(f"{j['company']} {j['title']} recruiter")
        linkedin_lead = f"https://www.linkedin.com/search/results/people/?keywords={encoded_query}"
        
        return f"""
        <div style="border-left: 4px solid #00E676; background-color: #f8f9fa; padding: 14px; margin-bottom: 15px; border-radius: 4px;">
            <h3 style="margin: 0 0 6px 0; color: #111;">{j['title']} &bull; <span style="color: #555;">{j['company']}</span></h3>
            <p style="margin: 4px 0; font-size: 14px;"><b>Location:</b> {j['location']} | <b>Comp:</b> {j['salary']}</p>
            <p style="margin: 4px 0; font-size: 14px;"><b>Visa Status:</b> <span style="color: #2e7d32; font-weight: bold;">{j['visa']}</span> | <b>Match:</b> <span style="color: #0d47a1; font-weight: bold;">{j['match_score']}%</span></p>
            <p style="margin: 4px 0; font-size: 13px; color: #444;"><i>{j['rationale']}</i></p>
            <div style="margin-top: 10px;">
                <a href="{j['url']}" style="background-color: #00E676; color: #000; padding: 6px 12px; text-decoration: none; font-weight: bold; border-radius: 4px; font-size: 13px; display: inline-block;">Apply Now</a>
                <a href="{linkedin_lead}" style="background-color: #0077B5; color: #fff; padding: 6px 12px; text-decoration: none; font-weight: bold; border-radius: 4px; font-size: 13px; display: inline-block; margin-left: 8px;">Locate Hiring Lead</a>
            </div>
        </div>
        """

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222; line-height: 1.5;">
        <h2 style="color: #000; margin-bottom: 2px;">GHOST Autonomous Reconnaissance Node</h2>
        <p style="color: #666; font-size: 14px; margin-top: 0;">Cycle Execution Complete &bull; 4-Hour Delta Synchronized</p>
        <p>A total of <b>{len(dispatched_jobs)}</b> verified roles matched your profile criteria and have been logged across your 3 target Google Sheets tabs.</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <h3 style="color: #111;">Top Strategic Opportunities Found</h3>
        {''.join([generate_job_card(job) for job in top_matches])}
        <br>
        <p style="font-size: 12px; color: #888;">GHOST Reconnaissance Engine &bull; HOLO_EARTH Autonomous Systems</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_content, 'html'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print("[GHOST] Executive briefing dispatched.")
    except Exception as e:
        print(f"[GHOST] Email error: {e}")

def main():
    print("[GHOST] Initializing reconnaissance stream...")
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    # Authenticate with Google
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ['SPREADSHEET_ID'])

    tab_names = ["Visa_Sponsorship", "Direct_Domestic_Undisclosed", "Paid_Remote_Internships"]
    worksheets = {}
    
    for tab in tab_names:
        try:
            worksheets[tab] = sheet.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet(title=tab, rows="2000", cols="12")
            ws.append_row(COLUMNS)
            worksheets[tab] = ws
            time.sleep(1.0)

    # Scrape and process candidates
    raw_jobs = fetch_job_stream()
    print(f"[GHOST] Parsed {len(raw_jobs)} candidate positions. Scoring against portfolio profile...")

    seen_urls = set()
    for ws in worksheets.values():
        try:
            urls = ws.col_values(9) # Application Link column
            seen_urls.update(urls)
        except Exception:
            pass

    logged_jobs = []
    tab_batches = {tab: [] for tab in tab_names}

    for job in raw_jobs:
        if job['url'] in seen_urls:
            continue

        match_score, match_rationale = calculate_portfolio_match(job['title'], job['description'])
        
        # Only accept relevant opportunities
        if match_score < 60:
            continue

        encoded_lead_query = urllib.parse.quote(f"{job['company']} {job['title']} recruiter")
        hiring_lead_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_lead_query}"

        row_payload = [
            today_str,
            job['title'],
            job['company'],
            job['location'],
            job['salary'],
            f"{match_score}%",
            match_rationale,
            job['visa'],
            job['url'],
            hiring_lead_url
        ]

        # Route to corresponding tab
        if job['is_intern'] and job['is_remote']:
            tab_batches["Paid_Remote_Internships"].append(row_payload)
        elif job['visa'] == "Sponsorship Offered":
            tab_batches["Visa_Sponsorship"].append(row_payload)
        else:
            tab_batches["Direct_Domestic_Undisclosed"].append(row_payload)

        seen_urls.add(job['url'])
        job['match_score'] = match_score
        job['rationale'] = match_rationale
        logged_jobs.append(job)

        # Cap batch to 20 highest-relevance jobs per run
        if len(logged_jobs) >= 20:
            break

    # Bulk insert rows into each tab
    for tab, rows in tab_batches.items():
        if rows:
            worksheets[tab].append_rows(rows, value_input_option='USER_ENTERED')
            print(f"[GHOST] Logged {len(rows)} opportunities into '{tab}'.")
            time.sleep(1.2)

    # Send summary email
    if logged_jobs:
        send_recon_email(logged_jobs, os.environ.get('GMAIL_USER'))
    else:
        print("[GHOST] No new distinct positions found in this 4-hour cycle.")

    print("[GHOST] Operational reconnaissance completed.")

if __name__ == "__main__":
    main()
              
