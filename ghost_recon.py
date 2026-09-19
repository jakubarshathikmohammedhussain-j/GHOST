import os
import json
import time
import re
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pandas as pd
from jobspy import scrape_jobs
from google.cloud import bigquery
from google.oauth2 import service_account

# ==========================================
# 1. SEARCH STREAMS & PARAMETERS
# ==========================================
SEARCH_TASKS = [
    # Visa Sponsorship Stream
    {"query": "visa sponsorship consulting", "loc": "United Kingdom"},
    {"query": "visa sponsorship strategy", "loc": "Singapore"},
    {"query": "tier 2 visa operations", "loc": "United Kingdom"},
    {"query": "visa sponsor associate", "loc": "Dubai, UAE"},
    {"query": "visa sponsorship consulting", "loc": "United States"},
    
    # Remote Paid Internship Stream
    {"query": "remote strategy intern", "loc": "Remote"},
    {"query": "remote operations internship", "loc": "United States"},
    {"query": "remote consulting intern", "loc": "United Kingdom"},
    
    # Direct Undisclosed / Early-Career Stream
    {"query": "strategy associate", "loc": "Dubai, UAE"},
    {"query": "junior consultant", "loc": "Singapore"},
    {"query": "operations associate", "loc": "United States"},
    {"query": "management trainee", "loc": "United Kingdom"}
]

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
def analyze_and_route_job(title, description, job_type, location, source_query):
    title_lower = title.lower()
    text_lower = f"{title_lower} {description.lower()} {source_query.lower()}"
    
    if any(banned in title_lower for banned in BANNED_TITLE_KEYWORDS):
        return 0, "Disqualified: Banned Domain/Rank", False, "No", None
    
    if ("senior" in title_lower or " sr " in f" {title_lower} " or "sr." in title_lower) and "junior" not in title_lower:
        return 0, "Disqualified: Senior Title", False, "No", None

    if any(exp in text_lower for exp in EXPERIENCE_OVERQUALIFIED):
        return 0, "Disqualified: 4+ Years Required", False, "No", None

    is_intern = "intern" in text_lower or (isinstance(job_type, str) and "intern" in job_type.lower())
    is_remote = "remote" in location.lower() or "remote" in text_lower or "work from home" in text_lower
    
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

    if visa_status == "Sponsorship Offered":
        assigned_bucket = "Visa Sponsorship"
        score = 90
        rationale = "PRIORITY: Verified Visa Sponsorship"
    elif is_intern and is_remote:
        assigned_bucket = "Paid Remote Internship"
        score = 85
        rationale = "PRIORITY: Remote Internship Position"
    else:
        assigned_bucket = "Direct Role (Undisclosed)"
        score = 75
        rationale = "Direct Strategy/Ops Role (0-3Y)"

    core_hits = [k for k in ["consulting", "strategy", "operations"] if k in text_lower]
    if core_hits:
        rationale += f" | Domain: {core_hits[0].title()}"

    return score, rationale, is_intern, visa_status, assigned_bucket

# ==========================================
# 3. RECRUITER EXTRACTION & LEAD ENGINE
# ==========================================
def extract_real_recruiter_email(description):
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

    hook_vector = "Maritime Chokepoint & Working Capital Drag" if "operations" in domain.lower() else "Macro Margin Compression & Strategy Automation"

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
# 4. BIGQUERY INGESTION ENGINE
# ==========================================
def stream_to_bigquery(client, table_id, rows_to_insert):
    if not rows_to_insert:
        return
    errors = client.insert_rows_json(table_id, rows_to_insert)
    if errors:
        print(f"[BIGQUERY ERROR] {errors}")
    else:
        print(f"[BIGQUERY] Successfully injected {len(rows_to_insert)} records.")

# ==========================================
# 5. EMAIL DISPATCH ENGINE
# ==========================================
def send_recon_email(jobs_payload, leads_payload):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_APP_PASSWORD')
    recipient_email = os.environ.get('RECIPIENT_EMAIL', sender_email)

    if not sender_email or not sender_password or (not jobs_payload and not leads_payload):
        return

    today_str = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
    msg = MIMEMultipart()
    msg['From'] = f"GHOST Intelligence Node <{sender_email}>"
    msg['To'] = recipient_email
    msg['Subject'] = f"GHOST Recon Alert: {len(jobs_payload)} Roles & {len(leads_payload)} Leads Pushed to BigQuery"

    html_content = f"""
    <div style="font-family: Arial, sans-serif; color: #111; max-width: 650px; line-height: 1.5;">
        <h2 style="color: #0d1117; margin-bottom: 4px;">GHOST Autonomous Recon: BigQuery Sync</h2>
        <p style="color: #586069; font-size: 13px; margin-top: 0;">Executed at {today_str} UTC | Target Corridors: Global</p>
        <hr style="border: 0; border-top: 1px solid #e1e4e8; margin: 16px 0;" />
        <p>Your BigQuery <b>market_signals</b> table has been synchronized with the latest labor intelligence.</p>
    """

    if jobs_payload:
        html_content += """
        <h3 style="color: #0366d6; margin-bottom: 8px;">Top Ingested Opportunities</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <tr style="background-color: #eaecef; text-align: left;">
                <th style="padding: 6px; border: 1px solid #d1d5da;">Job Title</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Company</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Location</th>
                <th style="padding: 6px; border: 1px solid #d1d5da;">Visa Status</th>
            </tr>
        """
        for job in jobs_payload[:6]:
            html_content += f"""
            <tr>
                <td style="padding: 6px; border: 1px solid #d1d5da;"><a href="{job['raw_data']['application_link']}" style="color: #0366d6; text-decoration: none;"><b>{job['raw_data']['job_title']}</b></a></td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job['entity_id']}</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job['raw_data']['location']}</td>
                <td style="padding: 6px; border: 1px solid #d1d5da;">{job['raw_data']['visa_status']}</td>
            </tr>
            """
        html_content += "</table>"

    html_content += """
        <p style="font-size: 12px; color: #586069; margin-top: 24px;">All records are queryable in HOLO-EARTH-CORE BigQuery Console.</p>
    </div>
    """

    msg.attach(MIMEText(html_content, 'html'))

    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        print(f"[GHOST Email] Alert successfully sent to {recipient_email}")
    except Exception as e:
        print(f"[GHOST Email] Dispatch failed: {e}")

# ==========================================
# 6. MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("[GHOST Recon] Initializing BigQuery Ingestion Engine...")
    
    # 1. Authenticate with BigQuery
    creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
    credentials = service_account.Credentials.from_service_account_info(creds_dict)
    client = bigquery.Client(credentials=credentials, project=creds_dict['project_id'])
    table_id = f"{creds_dict['project_id']}.telemetry_bronze.market_signals"

    seen_jobs = set()
    seen_companies = set()
    
    bq_jobs_payload = []
    bq_leads_payload = []

    # 2. Execute Searches
    for task in SEARCH_TASKS:
        query = task["query"]
        loc = task["loc"]
        try:
            print(f"[GHOST] Scanning: '{query}' in '{loc}'...")
            jobs_df = scrape_jobs(
                site_name=["linkedin", "indeed"],
                search_term=query,
                location=loc,
                results_wanted=15,
                hours_old=72,
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

                score, rationale, is_intern, visa, bucket = analyze_and_route_job(
                    title, desc, row.get('job_type', ''), location_val, query
                )
                
                if not bucket or score < 60:
                    continue
                
                timestamp_iso = datetime.utcnow().isoformat()

                # Package the Job Record for BigQuery
                job_record = {
                    "timestamp": timestamp_iso,
                    "domain": "GHOST",
                    "entity_id": company,
                    "signal_type": f"Labor Target: {bucket}",
                    "raw_data": {
                        "job_title": title,
                        "location": location_val,
                        "compensation": str(row.get('salary', 'N/A')),
                        "portfolio_match_score": score,
                        "strategic_rationale": rationale,
                        "visa_status": visa,
                        "application_link": link,
                        "search_query_used": query
                    }
                }
                bq_jobs_payload.append(job_record)
                seen_jobs.add(link)

                # Package the Recruiter Lead for BigQuery
                if company not in seen_companies and len(bq_leads_payload) < 5:
                    domain_match = "Strategy & Consulting"
                    for d in ["Operations", "Strategy", "Consulting"]:
                        if d.lower() in title.lower() or d.lower() in desc.lower():
                            domain_match = d
                            break

                    lead = synthesize_executive_lead(company, location_val, domain_match, desc)
                    lead_record = {
                        "timestamp": timestamp_iso,
                        "domain": "GHOST",
                        "entity_id": company,
                        "signal_type": "Executive Lead Extracted",
                        "raw_data": {
                            "practice_domain": lead['domain'],
                            "target_persona": lead['target_role'],
                            "extracted_email": lead['real_email'],
                            "corporate_pattern": lead['pattern'],
                            "xray_url": lead['xray_url'],
                            "hook_vector": lead['hook_vector']
                        }
                    }
                    bq_leads_payload.append(lead_record)
                    seen_companies.add(company)

        except Exception as e:
            print(f"[GHOST] Notice for '{query}': {e}")
            time.sleep(1.5)

    # 3. Stream payloads to BigQuery
    all_payloads = bq_jobs_payload + bq_leads_payload
    if all_payloads:
        stream_to_bigquery(client, table_id, all_payloads)
    else:
        print("[GHOST] No new high-priority roles found this cycle.")

    # 4. Dispatch Email Briefing
    send_recon_email(bq_jobs_payload, bq_leads_payload)
    print("[GHOST Recon] Pipeline Execution Complete.")

if __name__ == "__main__":
    main()
    
