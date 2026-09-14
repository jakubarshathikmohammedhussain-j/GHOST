import os
import json
import time
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from jobspy import scrape_jobs

# -------------------------------------------------------------
# CORE CONFIGURATION & HEURISTICS
# -------------------------------------------------------------
SEARCH_LOCATIONS = ["Chennai, India", "Dubai, UAE", "Singapore", "Europe", "Remote"]
SEARCH_TERMS = "consulting OR strategy OR operations OR product manager OR analytics OR ai"

COLUMNS = [
    "Date Found", "Job Title", "Company", "Location", "Compensation",
    "Portfolio Match (%)", "Strategic Match Rationale", "Visa Status",
    "Application Link", "Hiring Lead Search Query"
]

# Disqualifying title prefixes and executive ranks
BANNED_TITLE_KEYWORDS = [
    "vp", "vice president", "chief", "director", "head of", "principal", 
    "lead", "senior manager", "sr. manager", "managing consultant", 
    "partner", "general manager", "executive", "architect"
]

# Strict upper experience boundary
EXPERIENCE_OVERQUALIFIED = [
    "4+ years", "5+ years", "6+ years", "7+ years", "8+ years", 
    "10+ years", "5-7 years", "7-10 years", "minimum 5 years"
]

def analyze_job(title, description, job_type):
    title_lower = title.lower()
    text_lower = f"{title_lower} {description.lower()}"
    
    # 1. HARD GATEKEEPER: Instant disqualification on executive/senior titles
    if any(banned in title_lower for banned in BANNED_TITLE_KEYWORDS):
        return 0, "Disqualified: Executive / Senior Role", False, "No"
    
    # Handle "senior" or "sr" explicitly in title unless it's a junior tag
    if ("senior" in title_lower or " sr " in f" {title_lower} " or "sr." in title_lower) and "junior" not in title_lower:
        return 0, "Disqualified: Senior Title", False, "No"

    # 2. HARD GATEKEEPER: Disqualify high years of experience requirements
    if any(exp in text_lower for exp in EXPERIENCE_OVERQUALIFIED):
        return 0, "Disqualified: Requires 4+ Years Experience", False, "No"

    # 3. Detect Internship
    is_intern = "intern" in text_lower or (isinstance(job_type, str) and "intern" in job_type.lower())

    # 4. Detect Visa Sponsorship
    visa = "Undisclosed / Verify"
    if any(k in text_lower for k in ["visa sponsorship", "relocation support", "work permit provided", "sponsor visa", "visa supported"]):
        visa = "Sponsorship Offered"
    elif any(k in text_lower for k in ["no sponsorship", "citizens only", "must have right to work", "no visa"]):
        visa = "No Sponsorship"

    # 5. Calculate Match Score for 0-3Y Scope
    score = 25 # Lower base score to enforce strict qualification
    rationale = []

    # Early-Career Identifiers
    early_career_hits = ["junior", "entry", "associate", "graduate", "0-3", "0-2", "early career", "trainee", "analyst", "intern"]
    if any(k in title_lower for k in early_career_hits):
        score += 35
        rationale.append("Early-Career Title")
    elif any(k in text_lower for k in early_career_hits):
        score += 20
        rationale.append("Early-Career Scope")

    # Domain Alignment
    core_hits = [k for k in ["consulting", "strategy", "operations", "product", "supply chain", "logistics"] if k in text_lower]
    if core_hits:
        score += 20
        rationale.append(f"Domain: {core_hits[0].title()}")

    # Technical Alignment
    tech_hits = [k for k in ["python", "analytics", "sql", "ai", "tableau", "automation", "power bi"] if k in text_lower]
    if tech_hits:
        score += 20
        rationale.append("Tech/Analytics")

    final_score = min(max(score, 0), 98)
    return final_score, " | ".join(rationale) if rationale else "General Match", is_intern, visa
        
    return min(max(score, 10), 98), " | ".join(rationale) if rationale else "General Match", is_intern, visa

def send_recon_email(dispatched_jobs, recipient_email):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_APP_PASSWORD')
    if not sender_email or not sender_password or not dispatched_jobs: 
        return

    top_matches = sorted(dispatched_jobs, key=lambda x: x['match'], reverse=True)[:5]
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    msg = MIMEMultipart()
    msg['From'] = f"GHOST Recon Node <{sender_email}>"
    msg['To'] = recipient_email
    msg['Subject'] = f"GHOST Alert: {len(dispatched_jobs)} Strategic Positions Logged"

    html_content = f"""
    <div style="font-family: Arial, sans-serif; color: #222; line-height: 1.5; max-width: 600px;">
        <h2 style="color: #000; margin-bottom: 2px;">GHOST Autonomous Reconnaissance Node</h2>
        <p style="color: #666; font-size: 14px; margin-top: 0;">Cycle Execution Complete &bull; 4-Hour Delta Synchronized</p>
        <p><b>{len(dispatched_jobs)}</b> verified roles from Global Job Portals have been logged and auto-formatted.</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
    """
    
    for j in top_matches:
        html_content += f"""
        <div style="border-left: 4px solid #00E676; background-color: #f8f9fa; padding: 14px; margin-bottom: 15px; border-radius: 4px;">
            <h3 style="margin: 0 0 4px 0;">{j['title']} &bull; <span style="color: #555;">{j['company']}</span></h3>
            <p style="margin: 4px 0; font-size: 14px;"><b>Loc:</b> {j['location']} | <b>Comp:</b> {j['comp']}</p>
            <p style="margin: 4px 0; font-size: 14px;"><b>Match:</b> <span style="color: #0d47a1; font-weight: bold;">{j['match']}%</span> | <b>Visa:</b> <span style="color: #2e7d32; font-weight: bold;">{j['visa']}</span></p>
            <p style="margin: 4px 0; font-size: 13px; color: #444;"><i>{j['rationale']}</i></p>
            <div style="margin-top: 10px;">
                <a href="{j['url']}" style="background-color: #00E676; color: #000; padding: 6px 12px; text-decoration: none; font-weight: bold; border-radius: 4px; font-size: 13px; display: inline-block;">Apply Now</a>
                <a href="{j['lead_url']}" style="background-color: #0077B5; color: #fff; padding: 6px 12px; text-decoration: none; font-weight: bold; border-radius: 4px; font-size: 13px; display: inline-block; margin-left: 8px;">Locate Hiring Lead</a>
            </div>
        </div>
        """
    
    html_content += "</div>"
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
    print("[GHOST V2] Initializing Global Stealth Scraper Engine...")
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    # 1. Connect to Google Sheets
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
            ws = sheet.add_worksheet(title=tab, rows="2000", cols="10")
            ws.append_row(COLUMNS)
            worksheets[tab] = ws
            time.sleep(1)

    # 2. Scrape Jobs across multiple locations and platforms
    all_jobs_df = []
    
    # Defining the extended platform array
    target_platforms = ["linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "naukri", "bayt"]
    
    for loc in SEARCH_LOCATIONS:
        print(f"[GHOST] Scanning {loc} across global portals...")
        try:
            df = scrape_jobs(
                site_name=target_platforms,
                search_term=SEARCH_TERMS,
                location=loc,
                results_wanted=10, 
                hours_old=24, # Ensure fresh delta
                google_search_term=f"{SEARCH_TERMS} jobs in {loc} since yesterday"
            )
            if not df.empty:
                all_jobs_df.append(df)
            time.sleep(3) # Strict anti-ban pacing for web firewalls
        except Exception as e:
            print(f"[GHOST] Scraper error on {loc}: {e}")

    if not all_jobs_df:
        print("[GHOST] No new jobs found in this 4-hour cycle. Exiting.")
        return

    raw_jobs = pd.concat(all_jobs_df, ignore_index=True)
    
    # 3. Deduplication (Fetch already seen URLs)
    seen_urls = set()
    for ws in worksheets.values():
        try:
            seen_urls.update(ws.col_values(9))
        except:
            pass

    logged_jobs = []
    tab_batches = {tab: [] for tab in tab_names}

    # 4. Process, Score, and Route Jobs
    for _, job in raw_jobs.iterrows():
        url = str(job.get('job_url', ''))
        if url in seen_urls or not url or url.lower() == 'nan':
            continue

        title = str(job.get('title', ''))
        company = str(job.get('company', ''))
        desc = str(job.get('description', ''))
        loc = str(job.get('location', ''))
        
        # Format Compensation
        comp_min = job.get('min_amount')
        comp_max = job.get('max_amount')
        comp_curr = job.get('currency', '')
        if pd.notna(comp_min) and pd.notna(comp_max):
            comp = f"{comp_min} - {comp_max} {comp_curr}"
        elif pd.notna(comp_min):
            comp = f"{comp_min} {comp_curr}"
        else:
            comp = "Disclosed in Portal"
            
        job_type = str(job.get('job_type', ''))

        match_score, rationale, is_intern, visa = analyze_job(title, desc, job_type)
        
        # Strict relevance threshold
        if match_score < 55: 
            continue 

        # Generate LinkedIn Recruiter URL
        encoded_query = urllib.parse.quote(f"{company} {title} recruiter")
        lead_url = f"https://www.linkedin.com/search/results/people/?keywords={encoded_query}"

        row_payload = [
            today_str, title, company, loc, comp, f"{match_score}%", 
            rationale, visa, url, lead_url
        ]

        # Route to exact Tab
        if is_intern:
            tab_batches["Paid_Remote_Internships"].append(row_payload)
        elif visa == "Sponsorship Offered":
            tab_batches["Visa_Sponsorship"].append(row_payload)
        else:
            tab_batches["Direct_Domestic_Undisclosed"].append(row_payload)

        seen_urls.add(url)
        logged_jobs.append({
            "title": title, "company": company, "location": loc, 
            "comp": comp, "match": match_score, "visa": visa, 
            "url": url, "lead_url": lead_url, "rationale": rationale
        })
        
        # Hard cap to 20 jobs per 4-hour cycle to prevent sheet bloat
        if len(logged_jobs) >= 20: 
            break

    # 5. Log to Sheets and Auto-Resize Columns
    for tab, rows in tab_batches.items():
        if rows:
            ws = worksheets[tab]
            # Append data
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            print(f"[GHOST] Logged {len(rows)} opportunities into '{tab}'.")
            
            # API Payload to instantly auto-fit columns A through J
            try:
                sheet.batch_update({
                    "requests": [{
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": ws.id,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 10
                            }
                        }
                    }]
                })
            except Exception as e:
                print(f"[GHOST] Auto-resize warning for {tab}: {e}")

    # 6. Dispatch Email Briefing
    if logged_jobs:
        send_recon_email(logged_jobs, os.environ.get('GMAIL_USER'))
        print(f"[GHOST] Cycle Complete. {len(logged_jobs)} total roles secured.")
    else:
        print("[GHOST] No highly relevant roles passed the filter in this cycle.")

if __name__ == "__main__":
    main()
        
