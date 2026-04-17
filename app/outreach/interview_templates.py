"""
Super Lawyer Written Interview — 9-step campaign templates.

Steps 4 and 8 are internal actions (no email); all others generate draft emails.
Placeholders: {lawyer_name}, {your_name}, {questionnaire}, {interview_link}
"""

INTERVIEW_STEPS = {
    1: {
        'email_type': 'int_invite',
        'name': 'Interview Invite',
        'subject': 'Superlawyer Interview Invite',
        'body': (
            "Respected {lawyer_name},\n\n"
            "Warm greetings from SuperLawyer!\n\n"
            "We hope this message finds you well. We are reaching out to express our admiration for your remarkable professional journey in the field of Law. As we aim to showcase inspiring stories on our website www.superlawyer.in, a sister platform of LawSikho, we believe your experience could greatly motivate and benefit our readers.\n\n"
            "SuperLawyer, having reached over thousands of lawyers and law students, has expanded its global reach to include international law students and legal professionals. Your valuable insights and personal background could contribute significantly to our mission of inspiring and motivating aspiring legal professionals.\n\n"
            "Our platform boasts a diverse global audience, with readers hailing from India, the US, UK, UAE, and Canada. Featuring your journey on SuperLawyer would not only share your wealth of experience with a broad audience but also enhance visibility.\n\n"
            "We are genuinely interested in learning about your extensive experience in the legal profession, and it would be an absolute honor to include you among the accomplished professionals we have interviewed thus far.\n\n"
            "To proceed, we kindly request you to share your CV and an updated brief bio. This will enable our team to prepare a tailored questionnaire for your upcoming interview.\n\n"
            "Thank you for considering our invitation. We look forward to the opportunity to feature your inspiring story on SuperLawyer.\n\n"
            "Thanks and Regards,\n"
            "{your_name}\n"
            "SuperLawyer Team\n"
            "LinkedIn  - https://www.linkedin.com/company/superlawyer/\n"
            "SuperLawyer - www.superlawyer.in\n"
            "YouTube - https://www.youtube.com/channel/UCIdU1fAe1gmPguWuVStcPpg\n"
            "Instagram - https://www.instagram.com/super.lawyer/"
        ),
    },
    2: {
        'email_type': 'int_cv_ack',
        'name': 'CV/Bio Received',
        'subject': 'Re: SuperLawyer Interview — Thank You for Sharing Your Details',
        'body': (
            "Good Day {lawyer_name},\n\n"
            "I hope this message finds you well.\n\n"
            "Thank you for providing your details. We will be sharing the questionnaire with you shortly.\n\n"
            "Should you have any questions in the meantime, please don't hesitate to reach out.\n\n"
            "Thanks and Regards,\n"
            "{your_name}\n"
            "SuperLawyer Team"
        ),
    },
    3: {
        'email_type': 'int_cv_remind',
        'name': 'CV/Bio Reminder',
        'subject': 'Re: SuperLawyer Interview — CV/Bio Reminder',
        'body': (
            "Greetings {lawyer_name},\n\n"
            "Following up on our previous conversation, I'd be grateful if you could share your CV and brief bio at your earliest convenience. This will allow us to begin preparing the questionnaire for your interview.\n\n"
            "Thank you for your time and consideration. I look forward to receiving your details.\n\n"
            "Best regards,\n"
            "{your_name}\n"
            "SuperLawyer Team"
        ),
    },
    5: {
        'email_type': 'int_quest',
        'name': 'Send Questionnaire',
        'subject': 'Re: SuperLawyer Interview — Questionnaire',
        'body': (
            "Greetings from SuperLawyer!\n\n"
            "Thank you so much for kindly agreeing to share your invaluable insights and experiences with us. It is truly an honour to feature you on our platform.\n\n"
            "Please find the interview questions below. We encourage medium-length responses (approximately 8-10 lines per question), but there is no strict word limit. We also request you to kindly share a professional photograph of yourself along with your responses for publication purposes.\n\n"
            "Please note that minor refinements may be made to the responses to enhance readability and increase engagement. The final draft will be shared with you for approval if any changes are made; otherwise, it will be published as is.\n\n"
            "We would be grateful if you could submit the completed interview responses at your earliest convenience.\n\n"
            "Thanks and Regards,\n"
            "{your_name}\n"
            "SuperLawyer Team\n\n"
            "Questionnaire:\n\n"
            "{questionnaire}"
        ),
    },
    6: {
        'email_type': 'int_dl_remind',
        'name': 'Deadline Reminder',
        'subject': 'Re: SuperLawyer Interview — Gentle Reminder',
        'body': (
            "Good Day {lawyer_name},\n\n"
            "Hope you're doing well. This is just a gentle reminder regarding the interview responses we are awaiting from you.\n\n"
            "Please do let us know if you need any additional time or have any questions.\n\n"
            "Wishing you a great day ahead.\n\n"
            "Thanks and Regards,\n"
            "{your_name}\n"
            "SuperLawyer Team"
        ),
    },
    7: {
        'email_type': 'int_resp_ack',
        'name': 'Response Received',
        'subject': 'Re: SuperLawyer Interview — Thank You for Your Responses',
        'body': (
            "Good Day {lawyer_name},\n\n"
            "I wanted to take a moment to sincerely thank you for taking the time to provide us with your thoughtful and insightful interview responses. Your journey is truly inspiring, and we are excited to share it with our readers.\n\n"
            "We will be publishing the interview soon and will be sure to send you the link once it is live.\n\n"
            "Thank you once again for your valuable time and for sharing your experiences with us.\n\n"
            "Thanks and Regards,\n"
            "{your_name}\n"
            "SuperLawyer Team"
        ),
    },
    9: {
        'email_type': 'int_published',
        'name': 'Interview Published',
        'subject': 'Your SuperLawyer Interview is Live!',
        'body': (
            "Good Day {lawyer_name},\n\n"
            "I hope this message finds you well.\n\n"
            "I am pleased to inform you that your interview has been successfully published on our website. The answers have been shared exactly as you provided them.\n\n"
            "I kindly request that you review the published interview and share the post on LinkedIn, tagging our official page, SuperLawyer, as well as Ramanuj Mukherjee and {your_name}. Additionally, if you have an Instagram handle, please share it with us so we can tag you there as well.\n\n"
            "Thank you for your time and efforts.\n\n"
            "You can find your interview here: {interview_link}\n\n"
            "Best regards,\n"
            "{your_name}\n"
            "SuperLawyer Team"
        ),
    },
}

# Steps 4 and 8 are internal actions — shown in the UI but no email drafted
INTERVIEW_INTERNAL_STEPS = {
    4: {
        'name': 'Prepare Questionnaire',
        'description': (
            'Generate a tailored questionnaire using the Cinderella Arc Method. '
            'Click "Generate Step 5 Email" — it will auto-generate the questionnaire and embed it.'
        ),
    },
    8: {
        'name': 'Publish Interview',
        'description': (
            'Publish on WordPress (https://api.superlawyer.in/wp-login.php), '
            'LinkedIn (linkedin.com/company/superlawyer), '
            'and Instagram (https://www.instagram.com/super.lawyer/).'
        ),
    },
}

# Quick lookup: email_type → step metadata (includes step number)
INTERVIEW_EMAIL_TYPES: dict = {
    v['email_type']: {**v, 'step': k}
    for k, v in INTERVIEW_STEPS.items()
}

# Ordered list of all 9 steps for progress display
INTERVIEW_ALL_STEPS = [
    {'step': 1, 'name': 'Interview Invite',      'email_type': 'int_invite',    'internal': False},
    {'step': 2, 'name': 'CV/Bio Received',       'email_type': 'int_cv_ack',    'internal': False},
    {'step': 3, 'name': 'CV/Bio Reminder',       'email_type': 'int_cv_remind', 'internal': False},
    {'step': 4, 'name': 'Prepare Questionnaire', 'email_type': None,            'internal': True},
    {'step': 5, 'name': 'Send Questionnaire',    'email_type': 'int_quest',     'internal': False},
    {'step': 6, 'name': 'Deadline Reminder',     'email_type': 'int_dl_remind', 'internal': False},
    {'step': 7, 'name': 'Response Received',     'email_type': 'int_resp_ack',  'internal': False},
    {'step': 8, 'name': 'Publish Interview',     'email_type': None,            'internal': True},
    {'step': 9, 'name': 'Interview Published',   'email_type': 'int_published', 'internal': False},
]
