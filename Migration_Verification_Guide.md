# Zoho Books → GoHighLevel Migration  
## Verification Guide

**Document purpose:** This guide explains what was migrated from the Zoho Books Excel export and how to confirm the data appears correctly in GoHighLevel (GHL).

**Test migration source file:** `sample.xlsx`  
**Migration date:** _[Add date of your run]_  
**GHL sub-account:** _[Add location / business name]_

---

## 1. Summary of what was migrated

The migration imports six types of data from the Zoho Books export:

| # | Zoho data | GoHighLevel destination | Status in test run |
|---|-----------|-------------------------|-------------------|
| 1 | Contacts | GHL Contacts (name, email, phone, address, company) | ✅ 9 contacts created |
| 2 | Invoices | GHL Invoices (status **Issued**) | ✅ 3 invoices created |
| 3 | Payments | GHL Transactions (**Paid**, linked to invoices) | ✅ 3 payments recorded |
| 4 | Refunds | Refund tag + contact activity *(see note below)* | ✅ 2 refund records |
| 5 | Attachments (PDF/PNG) | Contact Documents | ⏸ Not included in this phase |
| 6 | Credit notes & activity logs | Contact timeline (notes/activity) | ✅ 2 credits; logs partial |

**Note on refunds:** When a refund is not linked to an invoice in the same export, GHL records it as a **contact activity** with the **Refund** tag. Full refund *transactions* in GHL require the original paid invoice to exist in the same account.

**Note on test file:** The sample export contains related **invoice + payment** rows for three customers. Other sheets (contacts, credits, refunds) represent **additional** customers not tied to those three invoices. That is expected for a pilot/sample file.

---

## 2. How to verify in GoHighLevel

### 2.1 Contacts (expect 9)

1. Open **Contacts** in GHL.
2. Search or filter by tag: **`zoho-import`** or **`zoho-books-import`**.
3. Confirm **nine** contacts appear, including:

| Customer name | Source in export |
|---------------|------------------|
| M. Etienne PHAM | Contact sheet |
| M. Maxime BÉLISLE THOMPSON | Contact sheet |
| Maé Campi | Invoice sheet |
| Mme Augustin Méliana | Invoice sheet |
| M. Boutin Marc | Invoice sheet |
| M. BELLAVANCE Justin | Credit note sheet |
| M. Nour BENZEKRI | Credit note sheet |
| M. Doré Aurélien | Refund sheet |
| M. JASARON Teejay | Refund sheet |

**What to check on sample contacts:**
- Name, email, and phone are present (some imports use placeholder contact details when Zoho had no email/phone).
- Tags include **`zoho-import`** and sheet-specific tags (e.g. `zoho-invoice`, `zoho-credit`, `zoho-refund`).
- Optional: custom fields such as *Zoho Invoice Number*, *Zoho Credit Note Number*, etc.

---

### 2.2 Invoices (expect 3 — Issued and Paid)

1. Open **Payments → Invoices** (or **Invoices**, depending on your GHL menu).
2. Locate these invoice numbers:

| Invoice number | Customer | Amount | Expected status |
|----------------|----------|--------|-----------------|
| **INV-000002** | Maé Campi | $200.00 CAD | Issued (Sent) — **Paid** |
| **INV-000003** | Mme Augustin Méliana | $200.00 CAD | Issued (Sent) — **Paid** |
| **INV-000004** | M. Boutin Marc | $200.00 CAD | Issued (Sent) — **Paid** |

**What to check:**
- Invoice is **not** in Draft.
- Status shows as **Sent** / Issued and **Paid**.
- Issue date matches Zoho (July 2021).
- Line description relates to inscription fees (2021 program).
- Customer on the invoice matches the name in the table above.

---

### 2.3 Payments / transactions (expect 3)

1. Open **Payments → Transactions**.
2. Filter by the invoices above or by the three invoice customers.

**What to check:**
- One **paid** transaction per invoice for **$200.00**.
- Each transaction is **linked to the correct invoice**.
- Invoice customers (Campi, Méliana, Boutin) may show tags **`Paid`** and **`zoho-payment`**.

---

### 2.4 Refunds (expect 2 — activity on contact)

1. Open contacts **M. Doré Aurélien** and **M. JASARON Teejay**.
2. Review the **contact timeline** / notes.

**What to check:**

| Credit note | Customer | Amount (from Zoho) |
|-------------|----------|-------------------|
| CN-00016 | M. Doré Aurélien | $2,150.00 |
| CN-00051 | M. JASARON Teejay | $15.00 |

- Timeline entry titled **Refund — CN-…** with refund details (date, mode, description).
- Contact tags include **`Refund`** and **`zoho-refund`**.

*These refunds are not tied to the three test invoices in the sample file; they appear as contact activity, not as invoice refund transactions.*

---

### 2.5 Credit notes (expect 2 — activity on contact)

1. Open contacts **M. BELLAVANCE Justin** and **M. Nour BENZEKRI**.
2. Review the **contact timeline**.

**What to check:**

| Credit note | Customer | Applied invoice (Zoho reference) |
|-------------|----------|----------------------------------|
| CN-00004 | M. BELLAVANCE Justin | INV-000440 |
| CN-00005 | M. Nour BENZEKRI | INV-000445 |

- Timeline entry **Credit Note — CN-…** with totals, dates, and status.
- Tag **`zoho-credit`** on the contact.

*Invoices INV-000440 and INV-000445 were not part of this sample export; only the credit note activity is visible in GHL.*

---

### 2.6 Activity log (sample file limitation)

The export’s **log** sheet had two rows:

| Log entry | Result in GHL |
|-----------|---------------|
| System backup completed | Skipped — no customer name in export |
| Invoice "INV-002530" updated (M. Sylvie Lucas) | Skipped — customer not included in this export |

This is **expected** for the sample file and does not indicate a failure of the invoice/contact migration.

---

## 3. Verification checklist

Use this checklist when signing off the test migration:

- [ ] **9 contacts** visible with `zoho-import` (or `zoho-books-import`) tag  
- [ ] **3 invoices** (INV-000002, INV-000003, INV-000004) — **Issued** and **Paid**  
- [ ] **3 payment transactions** at $200.00 each, linked to those invoices  
- [ ] **2 refund** activities on Doré Aurélien and JASARON Teejay with **Refund** tag  
- [ ] **2 credit note** activities on BELLAVANCE Justin and Nour BENZEKRI  
- [ ] Amounts and customer names match the Zoho export  
- [ ] Attachments — *not in scope for this phase*  

**Sign-off**

| Role | Name | Date | Approved (Y/N) |
|------|------|------|----------------|
| Client reviewer | | | |
| Technical lead | | | |

---

## 4. Mapping reference (Zoho → GoHighLevel)

| Zoho Books | GoHighLevel |
|------------|-------------|
| Contact / customer | Contact |
| Invoice (closed/paid) | Invoice (Issued → Sent, then Paid when payment applied) |
| Customer payment | Transaction via invoice payment |
| Refund | Refund tag + contact activity *(transaction when invoice link exists)* |
| Credit note | Contact activity (timeline note) |
| Activity log | Contact activity (when customer is in export) |
| PDF/PNG attachments | Contact documents *(planned — not in this delivery)* |

---

## 5. Known limitations (this phase)

1. **Sample export scope** — Not all sheets refer to the same customers; the three paid invoices are a self-contained test set.  
2. **Refunds** — Without the original paid invoice in GHL, refunds import as **tagged activity**, not payment refunds.  
3. **Credit notes** — Reference historical invoice numbers that may not exist in GHL until a full data export is migrated.  
4. **Attachments** — Document upload (filename matching / OCR) is **not** included yet.  
5. **Re-running the import** — Running the migration again on the same file can create **duplicate** contacts and invoices. Use a fresh export or deduplicate in GHL before a second run.

---

## 6. Recommended next steps

1. **Client review** — Complete the checklist in Section 3 in the GHL sub-account.  
2. **Full export** — Provide a complete Zoho Books Excel export (all customers, invoices, payments, refunds, credits) for production migration.  
3. **Attachments** — Confirm folder of PDF/PNG files and naming rules for Phase 2.  
4. **Production run** — Schedule migration after sign-off on this test and token/permissions check in GHL.

---

## 7. Support contacts

| Item | Detail |
|------|--------|
| Migration tool | `upload_to_ghl.py` (Zoho Excel → GHL API) |
| Test file | `sample.xlsx` |
| Questions | _[Your company name / email / phone]_ |

---

*This document describes verification for a test migration from Zoho Books to GoHighLevel. Update the migration date, sub-account name, and sign-off section before sending to the client.*
