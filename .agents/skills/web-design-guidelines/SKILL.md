---
name: web-design-guidelines
description: Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit design", "review UX", or "check my site against best practices".
metadata:
  author: vercel
  version: "1.0.0"
  argument-hint: <file-or-pattern>
---

# Web Interface Guidelines

Review files for compliance with Web Interface Guidelines.

## How It Works

1. Read the guidelines in `guidelines.md` (this folder)
2. Read the specified files (or prompt user for files/pattern)
3. Check against all rules in the guidelines
4. Output findings in the terse `file:line` format

## Guidelines Source

The rules are in `guidelines.md` next to this file — a saved copy of
`https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md`
(see `SOURCE.md` for the version). Read that file; do not fetch the URL at review time.

## Usage

When a user provides a file or pattern argument:
1. Read `guidelines.md`
2. Read the specified files
3. Apply all rules from the guidelines
4. Output findings using the format specified in the guidelines

If no files specified, ask the user which files to review.

## 주식 프로젝트 적용

향후 Next.js 대시보드의 접근성·키보드 포커스·표·폼·로딩/오류 상태를 검토한다.
한국어 문구에는 영어 Title Case 규칙을 적용하지 않는다. 실전/모의 상태와 시세 기준 시각은 색상 외 텍스트로도 구분한다.
현재 Phase 1은 API만 있으므로 Swagger를 완성된 대시보드로 평가하지 않는다.
