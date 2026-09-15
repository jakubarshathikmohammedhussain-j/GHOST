import os
import json
import time
import re
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from jobspy import scrape_jobs

# ==========================================
# CONFIGURATION & RECONNAISSANCE PARAMETERS
# ==========================================
SEARCH_LOCATIONS = ["Dubai, UAE", "United Kingdom", "Singapore", "United States", "Remote"]
SEARCH_TERMS = "consulting OR strategy OR operations"

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

BANNED_TITLE_KEYWORDS = [
    "vp", "vice president", "chief", "director", "head of", "principal", 
    "lead", "senior manager", "sr. manager", "managing consultant", 
    "partner", "general manager", "executive", "architect",
    "analyst", "engineer", "engineering", "developer" # Dropping Analyst & Engineering
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
# JOB HEURISTICS (STRICT 0-3Y & DOMAIN GATE)
# ==========================================
def analyze_job(title, description, job_type, location):
    title_lower = title.lower()
    text_lower = f"{title_lower} {description.lower()}"
    
    # 1. Gatekeeper: Drop Senior, Executive, Analyst, and Engineering titles
    if any(banned in title_lower for banned in BANNED_TITLE_KEYWORDS):
        return 0, "Disqualified: Banned Domain/Rank", False, "No"
    
    if ("senior" in title_lower or " sr " in f" {title_lower} " or "sr." in title_lower) and "junior" not in title_lower:
        return 0, "Disqualified: Senior Title", False, "No"

    # 2. Gatekeeper: Drop high experience barriers
    if any(exp in text_lower for exp in EXPERIENCE_OVERQUALIFIED):
        return 0, "Disqualified: 4+ Years Required", False, "No"

    # Status Identifiers
    is_intern = "intern" in text_lower or (isinstance(job_type, str) and "intern" in job_type.lower())
    is_remote = "remote" in location.lower() or "remote" in text_lower or "work from home" in text_lower
    
    visa = "Undisclosed / Verify"
    if any(k in text_lower for k in ["visa sponsorship", "relocation support", "work permit provided", "sponsor visa", "visa supported"]):
        visa = "Sponsorship Offered"
    elif any(k in text_lower for k in ["no sponsorship", "citizens only", "must have right to work"]):
        visa = "No Sponsorship"

    # Base Score
    score = 20
    rationale = []

    # PRIORITY 1: Visa Sponsorship (+50)
    if visa == "Sponsorship Offered":
        score += 50
        rationale.append("PRIORITY: Visa Sponsored")

    # PRIORITY 2: Remote Paid Internship (+40)
    if is_intern and is_remote and "unpaid" not in text_lower:
        score += 40
        rationale.append("PRIORITY: Remote Paid Intern")

    # Domain Alignment (+20)
    core_hits = [k for k in ["consulting", "strategy", "operations"] if k in text_lower]
    if core_hits:
        score += 20
        rationale.append(f"Domain: {core_hits[0].title()}")

    final_score = min(max(score, 0), 99)
    return final_score, " | ".join(rationale) if rationale else "General Match", is_intern, visa

# ==========================================
# RECRUITER EXTRACTION & LEAD ENGINE
# ==========================================
def extract_real_recruiter_email(description):
    """Scans raw JD text for emails and drops generic HR inboxes."""
    emails_found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', description)
    real_emails = [e for e in emails_found if not any(dummy in e.lower() for dummy in GENERIC_EMAILS)]
    return real_emails[0] if real_emails else "None Found in Text"

def synthesize_executive_lead(company, location, domain, description):
    company_clean = company.strip()
    company_lower = company_clean.lower()
    
    # Attempt true email extraction
    real_email = extract_real_recruiter_email(description)

    # Identify email domain pattern fallback
    pattern = "firstname.lastname@company.com"
    for k, v in CORPORATE_EMAIL_SYNTAX.items():
        if k in company_lower:
            pattern = v
            break
            
    # Target Persona Selection
    if any(tier in company_lower for tier in ["mckinsey", "bcg", "bain"]):
        target_role = "Engagement Manager / Associate Partner"
    elif any(tier in company_lower for tier in ["deloitte", "pwc", "ey", "kpmg", "accenture", "strategy&"]):
        target_role = "Senior Manager / Strategy Practice Lead"
    else:
        target_role = "Director of Strategy & Operations / Talent Acquisition Lead"

    xray_query = f'site:linkedin.com/in ("{target_role.split(" / ")[0]}" OR "{target_role.split(" / ")[-1]}") "{company_clean}" "{location}"'
    xray_url = f"https://www.google.com/search?q={urllib.parse.quote(xray_query)}"

    if "operations" in domain.lower():
        hook_vector = "Maritime Chokepoint & Working Capital Drag"
    else:
        hook_vector = "Macro Margin Compression & Consulting Automation"

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
# MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("[GHOST Recon] Commencing 4-Hour Pipeline: Priority Scan (Visa/Remote) -> Target Domains...")
    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ['SPREADSHEET_ID'])

    try:
        ws_jobs = sheet.worksheet("Tactical_Recon_0-3Y")
    except gspread.exceptions.WorksheetNotFound:
        ws_jobs = sheet.add_worksheet(title="Tactical_Recon_0-3Y", rows="1000", cols="10")
        ws_jobs.append_row(COLUMNS_JOBS)

    try:
        ws_leads = sheet.worksheet("Recruiter_Leads")
    except gspread.exceptions.WorksheetNotFound:
        ws_leads = sheet.add_worksheet(title="Recruiter_Leads", rows="1000", cols="9")
        ws_leads.append_row(COLUMNS_LEADS)

    seen_jobs = set(ws_jobs.col_values(9))
    seen_companies = set(ws_leads.col_values(2))

    new_jobs_batch = []
    new_leads_batch = []

    for loc in SEARCH_LOCATIONS:
        try:
            print(f"[GHOST] Scanning: {loc}...")
            jobs_df = scrape_jobs(
                site_name=["linkedin", "indeed", "glassdoor"],
                search_term=SEARCH_TERMS,
                location=loc,
                results_wanted=15,
                hours_old=24,
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

                if link in seen_jobs or not title or not company:
                    continue

                # Run Hard Gatekeeper & Priority Scorer
                score, rationale, is_intern, visa = analyze_job(title, desc, row.get('job_type', ''), location_val)
                if score < 50:
                    continue

                # Temp dict for sorting later
                new_jobs_batch.append({
                    "score": score,
                    "row_data": [
                        today_str, title, company, location_val, str(row.get('salary', 'N/A')),
                        f"{score}%", rationale, visa, link,
                        f'site:linkedin.com/in "{company}" "recruiter" "{location_val}"'
                    ]
                })
                seen_jobs.add(link)

                # Process Company for Recruiter Lead Generation (Max 5 per 4 hours)
                if company not in seen_companies and len(new_leads_batch) < 5:
                    domain_match = "Strategy & Consulting"
                    for d in ["Operations", "Strategy", "Consulting"]:
                        if d.lower() in title.lower() or d.lower() in desc.lower():
                            domain_match = d
                            break

                    lead = synthesize_executive_lead(company, location_val, domain_match, desc)
                    new_leads_batch.append([
                        today_str, lead['company'], lead['domain'],
                        lead['target_role'], lead['real_email'], lead['pattern'],
                        lead['xray_url'], lead['hook_vector'], "Queued"
                    ])
                    seen_companies.add(company)

        except Exception as e:
            print(f"[GHOST] Scraper notice for {loc}: {e}")

    # Sort jobs by Priority Score (Highest first) before appending
    if new_jobs_batch:
        new_jobs_batch = sorted(new_jobs_batch, key=lambda x: x['score'], reverse=True)
        jobs_to_append = [job['row_data'] for job in new_jobs_batch]
        ws_jobs.append_rows(jobs_to_append, value_input_option='USER_ENTERED')
        print(f"[GHOST] Committed {len(jobs_to_append)} prioritized roles.")

    if new_leads_batch:
        ws_leads.append_rows(new_leads_batch, value_input_option='USER_ENTERED')
        print(f"[GHOST] Committed {len(new_leads_batch)} Recruiter Leads.")

    print("[GHOST] 4-Hour Cycle Complete.")

if __name__ == "__main__":
    main()
    
