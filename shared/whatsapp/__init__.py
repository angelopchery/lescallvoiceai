"""WhatsApp follow-up messages — Integrations → WhatsApp.

A caller agrees on a call to receive something on WhatsApp; the tenant's
PLAYBOOK says what (an approved template, an attachment, a link) and whether a
person must approve it first. Modules:

    config       env knobs, read at call time (kill switch, secrets, base URL)
    crypto       Fernet for the stored access token
    placeholders {contact_name}/{title}/{greeting}/… resolution for one call
    templates    Meta template components → slots, preview, send payload
    consent      did THIS call grant (or refuse) WhatsApp contact?
    store        every SQL statement the feature runs
    stage        the intelligence-pipeline stage that creates sends
    sender       the loop that delivers queued sends
    graph        the Meta Graph API client
    interakt     the Interakt (Meta BSP) client — the other way a tenant's
                 number can be connected (RUNBOOK §80)
    webhook      delivery statuses, replies, STOP, template reviews
"""
