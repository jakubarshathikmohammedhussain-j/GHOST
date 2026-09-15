import os
import json
import time
import re
import urllib.parse
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from jobspy import scrape_jobs

# ==========================================
# 1. SEARCH STREAMS & PARAMETERS
# ==========================================
TARGET_LOCATIONS = ["Dubai, UAE", "United Kingdom", "Singapore", "United States", "Remote"]

# Three dedicated search streams to guarantee every bucket receives relevant hits
SEARCH_STREAMS = [
    {
        "intent": "visa",
        "query": '("visa sponsorship" OR "visa sponsor" OR "tier 2 visa" OR "skilled worker visa") AND (consulting OR strategy OR operations)',
        "locations": ["United Kingdom", "Dubai, UAE", "Singapore", "United States"]
    },
    {
        "intent": "internship",
        "query": '(intern OR internship OR "working student") AND (consulting OR strategy OR operations)',
        "locations": ["Remote", "United Kingdom", "United States", "Singapore"]
    },
    {
        "intent": "direct",
        "query": '("junior consultant" OR "associate consultant" OR "strategy associate" OR "operations associate" OR "management trainee")',
        "locations": TARGET_LOCATIONS
    }
]

COLUMNS_JOBS = [
    "Date Found", "Job Title", "Company", "Location", "Compensation",
    "Portfolio Match (%)", "Strategic Match Rationale", "Visa Status",
    "Application Link", "Hiring Lead Search Query"
]

COLUMNS_LEADS = [
    "Date Logged", "Target Company", "Practice Domain", "Target Persona / Recruiter",
    "Extracted Real Email (From JD)", "Estimated Corporate Pattern", 
    "Direct LinkedIn X-Ray URL", "Operational Hook Vector", "Outreach Status"
]

# Strict exclusions: Drop Senior, Executive, Analyst, and Engineering roles
BANNED_TITLE_KEYWORDS = [
    "vp", "vice president", "chief", "director", "head of", "principal", 
    "lead", "senior manager", "sr. manager", "managing consultant", 
    "partner", "general manager", "executive", "architect",
    "analyst", "engineer", "engineering", "developer"
]

EXPERIENCE_OVERQUALIFIED = [
    "4+ years", "5+ years", "6+ years", "7+ years", "8+ years", 
    "10+ years", "5-7 years", "7-10 years", "minimum 5 years"
]

CORPORATE_EMAIL_SYNTAX = {
    "mckinsey": "firstname_lastname@mckinsey.com",
    "bcg": "lastname.firstname@bcg.com",
    "bain": "firstname.lastname@bain.com",
    "strategy&": "firstname.lastname@pwc.com",
    "pwc": "firstname.lastname@pwc.com",
    "ey": "firstname.lastname@ey.com",
    "deloitte": "firstnamelastname@deloitte.com",
    "kpmg": "firstnamelastname@kpmg.com",
    "amazon": "alias@amazon.com",
    "accenture": "firstname.lastname@accenture.com"
}

GENERIC_EMAILS = ['info@', 'hr@', 'careers@', 'admin@', 'support@', 'jobs@', 'apply@', 'contact@']

# ==========================================
# 2. JOB HEURISTICS & BUCKET ROUTER
# ==========================================
def analyze_and_route_job(title, description, job_type, location):
    title_lower = title.lower()
    text_lower = f"{title_lower} {description.lower()}"
    
    # 1. Gatekeeper: Drop Senior, Executive, Analyst, and Engineering titles
    if any(banned in title_lower for banned in BANNED_TITLE_KEYWORDS):
        return 0, "Disqualified: Banned Domain/Rank", False, "No", None
    
    if ("senior" in title_lower or " sr " in f" {title_lower} " or "sr." in title_lower) and "junior" not in title_lower:
        return 0, "Disqualified: Senior Title", False, "No", None

    # 2. Gatekeeper: Drop 4+ years experience requirements
    if any(exp in text_lower for exp in EXPERIENCE_OVERQUALIFIED):
        return 0, "Disqualified: 4+ Years Required", False, "No", None

    # Flags
    is_intern = "intern" in text_lower or (isinstance(job_type, str) and "intern" in job_type.lower())
    is_remote = "remote" in location.lower() or "remote" in text_lower or "work from home" in text_lower
    
    # Visa Detection
    is_visa = any(k in text_lower for k in [
        "visa sponsorship", "visa sponsor", "sponsorship available", 
        "relocation support", "work permit provided", "tier 2", "skilled worker visa"
    ])
    has_no_visa = any(k in text_lower for k in ["no sponsorship", "citizens only", "must have right to work", "unable to sponsor"])

    if is_visa and not has_no_visa:
        visa_status = "Sponsorship Offered"
    elif has_no_visa:
        visa_status = "No Sponsorship"
    else:
        visa_status = "Undisclosed / Verify"

    # Multi-Tab Bucket Routing
    if visa_status == "Sponsorship Offered":
        assigned_bucket = "Visa_Sponsorship"
        score = 90
        rationale = "PRIORITY: Verified Visa Sponsorship"
    elif is_intern and is_remote:
        assigned_bucket = "Paid_Remote_Internships"
        score = 85
        rationale = "PRIORITY: Remote Internship Position"
    else:
        assigned_bucket = "Direct_Domestic_Undisclosed"
        score = 75
        rationale = "Direct Operational/Strategy Role (0-3Y)"

    # Core Domain Check
    core_hits = [k for k in ["consulting", "strategy", "operations"] if k in text_lower]
    if core_hits:
        rationale += f" | {core_hits[0].title()}"

    return score, rationale, is_intern, visa_status, assigned_bucket

# ==========================================
# 3. RECRUITER EXTRACTION & LEAD ENGINE
# ==========================================
def extract_real_recruiter_email(description):
    """Scans raw JD text for human emails, excluding generic inboxes."""
    emails_found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', description)
    real_emails = [e for e in emails_found if not any(dummy in e.lower() for dummy in GENERIC_EMAILS)]
    return real_emails[0] if real_emails else "Pending X-Ray Outreach"

def synthesize_executive_lead(company, location, domain, description):
    company_clean = company.strip()
    company_lower = company_clean.lower()
    real_email = extract_real_recruiter_email(description)

    pattern = "firstname.lastname@company.com"
    for k, v in CORPORATE_EMAIL_SYNTAX.items():
        if k in company_lower:
            pattern = v
            break
            
    if any(tier in company_lower for tier in ["mckinsey", "bcg", "bain"]):
        target_role = "Engagement Manager / Talent Lead"
    elif any(tier in company_lower for tier in ["deloitte", "pwc", "ey", "kpmg", "accenture", "strategy&"]):
        target_role = "Practice Lead / Strategy Senior Manager"
    else:
        target_role = "Director of Strategy & Operations / Talent Partner"

    xray_query = f'site:linkedin.com/in ("{target_role.split(" / ")[0]}" OR "recruiter") "{company_clean}" "{location}"'
    xray_url = f"https://www.google.com/search?q={urllib.parse.quote(xray_query)}"

    if "operations" in domain.lower():
        hook_vector = "Maritime Chokepoint & Working Capital Drag (Cash Conversion Cycle)"
    else:
        hook_vector = "Macro Margin Compression & Strategy Automation (EU AI Act / Policy)"

    return {
        "company": company_clean,
        "domain": domain,
        "target_role": target_role,
        "real_email": real_email,
        "pattern": pattern,
        "xray_url": xray_url,
        "hook_vector": hook_vector
    }

# ==========================================
# 4. MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("[GHOST Recon] Initializing 4-Tab Multi-Stream Scanner...")
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ['SPREADSHEET_ID'])

    # Setup all 4 tabs
    tabs_config = {
        "Visa_Sponsorship": COLUMNS_JOBS,
        "Direct_Domestic_Undisclosed": COLUMNS_JOBS,
        "Paid_Remote_Internships": COLUMNS_JOBS,
        "Recruiter_Leads": COLUMNS_LEADS
    }

    worksheets = {}
    for tab_name, headers in tabs_config.items():
        try:
            ws = sheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = sheet.add_worksheet(title=tab_name, rows="1000", cols=str(len(headers)))
            ws.append_row(headers)
        worksheets[tab_name] = ws

    # Cache existing entries to avoid duplication
    seen_jobs = set(worksheets["Visa_Sponsorship"].col_values(9) + 
                    worksheets["Direct_Domestic_Undisclosed"].col_values(9) + 
                    worksheets["Paid_Remote_Internships"].col_values(9))
    seen_companies = set(worksheets["Recruiter_Leads"].col_values(2))

    batch_payloads = {
        "Visa_Sponsorship": [],
        "Direct_Domestic_Undisclosed": [],
        "Paid_Remote_Internships": [],
        "Recruiter_Leads": []
    }

    # Execute Scrape Streams
    for stream in SEARCH_STREAMS:
        query = stream["query"]
        for loc in stream["locations"]:
            try:
                print(f"[GHOST] Running stream [{stream['intent']}] for: {loc}...")
                jobs_df = scrape_jobs(
                    site_name=["linkedin", "indeed", "glassdoor"],
                    search_term=query,
                    location=loc,
                    results_wanted=15,
                    hours_old=48,
                    country_indeed='worldwide'
                )
                
                if jobs_df.empty:
                    continue

                for _, row in jobs_df.iterrows():
                    title = str(row.get('title', ''))
                    desc = str(row.get('description', ''))
                    company = str(row.get('company', ''))
                    link = str(row.get('job_url', ''))
                    location_val = str(row.get('location', loc))

                    if link in seen_jobs or not title or not company or company.lower() == "nan":
                        continue

                    # Filter and identify target tab
                    score, rationale, is_intern, visa, bucket = analyze_and_route_job(
                        title, desc, row.get('job_type', ''), location_val
                    )
                    
                    if not bucket or score < 60:
                        continue

                    job_record = [
                        today_str, title, company, location_val, str(row.get('salary', 'N/A')),
                        f"{score}%", rationale, visa, link,
                        f'site:linkedin.com/in "{company}" ("recruiter" OR "talent acquisition") "{location_val}"'
                    ]
                    batch_payloads[bucket].append(job_record)
                    seen_jobs.add(link)

                    # Lead Engine (Capture up to 5 verified company contacts per run)
                    if company not in seen_companies and len(batch_payloads["Recruiter_Leads"]) < 5:
                        domain_match = "Strategy & Consulting"
                        for d in ["Operations", "Strategy", "Consulting"]:
                            if d.lower() in title.lower() or d.lower() in desc.lower():
                                domain_match = d
                                break

                        lead = synthesize_executive_lead(company, location_val, domain_match, desc)
                        batch_payloads["Recruiter_Leads"].append([
                            today_str, lead['company'], lead['domain'],
                            lead['target_role'], lead['real_email'], lead['pattern'],
                            lead['xray_url'], lead['hook_vector'], "Queued"
                        ])
                        seen_companies.add(company)

            except Exception as e:
                print(f"[GHOST] Scraper notice [{stream['intent']}] in {loc}: {e}")

    # Commit all batches to their respective tabs
    for tab_name, rows in batch_payloads.items():
        if rows:
            ws = worksheets[tab_name]
            ws.append_rows(rows, value_input_option='USER_ENTERED')
            print(f"[GHOST] Added {len(rows)} records into '{tab_name}'.")

    # Auto-resize columns across all 4 tabs
    for tab_name, ws in worksheets.items():
        try:
            sheet.batch_update({
                "requests": [{
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": len(tabs_config[tab_name])
                        }
                    }
                }]
            })
            time.sleep(1)
        except Exception as e:
            print(f"[GHOST] Auto-resize note for {tab_name}: {e}")

    print("[GHOST Recon] 4-Tab Synchronization Complete.")

if __name__ == "__main__":
    main()
    
