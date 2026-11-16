"""Check whether an issue follows the corresponding issue form template.

GitHub issue forms (`.github/ISSUE_TEMPLATE/*.yml`) are only rendered by the web
UI. Issues created through the REST/GraphQL API (for example by AI agents or
`gh issue create`) are not validated by GitHub at all: no title prefix, no
labels and no required sections. `blank_issues_enabled: false` has the same
limitation, it only removes the "Open a blank issue" entry from the web UI.

This script closes that gap on the server side. It is triggered on the
`issues: [opened, edited]` event, derives the required sections from the
template that matches the issue title, and labels + comments on the issue when
the body does not conform.
"""

import json
import os
import re
import sys

import yaml
from github import Auth, Github, GithubException

COMMENT_MARKER = '<!-- issue-template-check -->'
LABEL_NAME = 'template_missing'
LABEL_COLOR = 'd73a4a'
LABEL_DESCRIPTION = 'Issue 未按模板填写，缺少模板要求的必填内容'

TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ISSUE_TEMPLATE')
)
# GitHub issue forms render an empty optional field as "_No response_".
PLACEHOLDER_VALUES = {'', '_no response_', 'no response', '无', '暂无', 'none', 'n/a'}
HTML_COMMENT_RE = re.compile(r'<!--[\s\S]*?-->')
HEADING_RE = re.compile(r'^###\s+(.*\S)\s*$')


def normalize(text):
    """Normalize a heading/label for comparison."""
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


def is_empty(content):
    cleaned = HTML_COMMENT_RE.sub('', content or '')
    cleaned = cleaned.strip().strip('*_`').strip()
    return cleaned.lower() in PLACEHOLDER_VALUES


def load_templates():
    """Load issue form templates and extract their required fields."""
    templates = []
    for filename in sorted(os.listdir(TEMPLATE_DIR)):
        if not filename.endswith(('.yml', '.yaml')) or filename == 'config.yml':
            continue
        with open(os.path.join(TEMPLATE_DIR, filename), encoding='utf-8') as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or not data.get('title'):
            continue

        required_fields = []
        for item in data.get('body') or []:
            if not isinstance(item, dict):
                continue
            attributes = item.get('attributes') or {}
            label = (attributes.get('label') or '').strip()
            if not label:
                continue
            if item.get('type') == 'checkboxes':
                required_options = [
                    (option.get('label') or '').strip()
                    for option in attributes.get('options') or []
                    if isinstance(option, dict) and option.get('required')
                ]
                if required_options:
                    required_fields.append({
                        'label': label,
                        'type': 'checkboxes',
                        'required_options': required_options,
                    })
            elif (item.get('validations') or {}).get('required'):
                required_fields.append({'label': label, 'type': item.get('type')})

        templates.append({
            'file': filename,
            'name': data.get('name') or filename,
            'title': str(data['title']),
            'required_fields': required_fields,
        })
    return templates


def title_matches(issue_title, template_title):
    prefix = template_title.strip()
    return bool(prefix) and issue_title.strip().lower().startswith(prefix.lower())


def parse_sections(body):
    """Split a rendered issue body into `### heading -> content` mapping."""
    sections = {}
    current = None
    buffer = []
    for line in (body or '').splitlines():
        match = HEADING_RE.match(line)
        if match:
            if current is not None:
                sections[current] = '\n'.join(buffer).strip()
            current = match.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = '\n'.join(buffer).strip()
    return {normalize(key): value for key, value in sections.items()}


def is_option_checked(content, option):
    pattern = r'^\s*-\s*\[[xX]\]\s*' + re.escape(option) + r'\s*$'
    return re.search(pattern, content or '', re.MULTILINE) is not None


def find_missing_fields(body, template):
    """Return the list of required template fields missing from the body."""
    sections = parse_sections(body)
    missing = []
    for field in template['required_fields']:
        content = sections.get(normalize(field['label']))
        if content is None:
            missing.append(field['label'])
            continue
        if field['type'] == 'checkboxes':
            for option in field['required_options']:
                if not is_option_checked(content, option):
                    missing.append('{} → {}'.format(field['label'], option))
        elif is_empty(content):
            missing.append(field['label'])
    return missing


def detect_language(text):
    return 'zh' if re.search(r'[\u4e00-\u9fa5]', text or '') else 'en'


def build_comment(repo_full_name, language, template, missing, candidates):
    new_issue_url = 'https://github.com/{}/issues/new?template={}'.format(
        repo_full_name, template['file'] if template else ''
    )
    if language == 'zh':
        lines = [
            COMMENT_MARKER,
            '## ⚠️ Issue 未按模板填写',
            '',
            'GitHub 的 Issue 表单只在网页端生效，通过 API（如 AI 助手、`gh issue create`）直接创建的 '
            'Issue 不会被校验。为保证问题可被有效跟进，本仓库在 CI 中做了服务端校验，当前 Issue 缺少以下模板内容：',
            '',
        ]
        lines += ['- [ ] `{}`'.format(item) for item in missing]
        if template:
            lines += ['', '对应模板：**{}**（[使用该模板新建 Issue]({})）'.format(
                template['name'], new_issue_url)]
        else:
            lines += ['', '可用模板：{}'.format('、'.join(c['name'] for c in candidates))]
        lines += [
            '',
            '请补充上述内容（标题前缀需与模板一致，如 `[Bug] `、`[疑问] `），'
            '直接编辑 Issue 正文即可自动重新检查。',
        ]
        return '\n'.join(lines)

    lines = [
        COMMENT_MARKER,
        '## ⚠️ Issue does not follow the issue template',
        '',
        'GitHub issue forms are only rendered by the web UI. Issues created directly through the '
        'API (AI agents, `gh issue create`, ...) are not validated by GitHub, so this repository '
        'performs a server-side check in CI. The following required template fields are missing:',
        '',
    ]
    lines += ['- [ ] `{}`'.format(item) for item in missing]
    if template:
        lines += ['', 'Template: **{}** ([create an issue with this template]({}))'.format(
            template['name'], new_issue_url)]
    else:
        lines += ['', 'Available templates: {}'.format(', '.join(c['name'] for c in candidates))]
    lines += [
        '',
        'Please fill in the fields above (the title prefix must match the template, e.g. `[Bug] `, '
        '`[Question] `). Editing the issue body triggers a re-check automatically.',
    ]
    return '\n'.join(lines)


def ensure_label(repo):
    try:
        return repo.get_label(LABEL_NAME)
    except GithubException as error:
        if error.status != 404:
            raise
        return repo.create_label(LABEL_NAME, LABEL_COLOR, LABEL_DESCRIPTION)


def is_own_comment(comment, bot_login):
    # 只维护本脚本自己发的评论，避免用户手动写入 marker 导致评论被误删/误改。
    return COMMENT_MARKER in (comment.body or '') and comment.user.login == bot_login


def upsert_comment(issue, body, bot_login):
    for comment in issue.get_comments():
        if is_own_comment(comment, bot_login):
            comment.edit(body)
            print('Updated template check comment.')
            return
    issue.create_comment(body)
    print('Created template check comment.')


def remove_comment(issue, bot_login):
    for comment in issue.get_comments():
        if is_own_comment(comment, bot_login):
            comment.delete()
            print('Removed template check comment.')


def main():
    with open(os.getenv('GITHUB_EVENT_PATH'), encoding='utf-8') as f:
        event = json.load(f)

    issue_payload = event['issue']
    repo_full_name = event['repository']['full_name']
    issue_number = issue_payload['number']
    issue_title = issue_payload.get('title') or ''
    issue_body = issue_payload.get('body') or ''
    language = detect_language(issue_title + issue_body)

    github = Github(auth=Auth.Token(os.getenv('GITHUB_TOKEN')))
    bot_login = github.get_user().login
    repo = github.get_repo(repo_full_name)
    issue = repo.get_issue(number=issue_number)

    templates = load_templates()
    candidates = [t for t in templates if title_matches(issue_title, t['title'])]

    if not candidates:
        # A title without any known template prefix is itself a bypass signal.
        best_template = None
        missing = ['标题前缀 / Title prefix: one of {}'.format(
            ', '.join('`{}`'.format(t['title'].strip()) for t in templates)
        )]
    else:
        best_template, missing = None, None
        for template in candidates:
            template_missing = find_missing_fields(issue_body, template)
            if not template_missing:
                best_template, missing = template, []
                break
            if missing is None or len(template_missing) < len(missing):
                best_template, missing = template, template_missing

    labels = {label.name for label in issue.labels}
    if missing:
        ensure_label(repo)
        upsert_comment(issue, build_comment(
            repo_full_name, language, best_template, missing, templates
        ), bot_login)
        if LABEL_NAME not in labels:
            issue.add_to_labels(LABEL_NAME)
        print('Template check failed, missing: {}'.format('; '.join(missing)))
        sys.exit(1)

    remove_comment(issue, bot_login)
    if LABEL_NAME in labels:
        issue.remove_from_labels(LABEL_NAME)
    print('Template check passed.')


if __name__ == '__main__':
    main()
