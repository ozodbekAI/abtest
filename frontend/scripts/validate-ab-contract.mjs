import fs from 'node:fs';

const client = fs.readFileSync(new URL('../src/api/client.ts', import.meta.url), 'utf8');
const types = fs.readFileSync(new URL('../src/types.ts', import.meta.url), 'utf8');
const wizard = fs.readFileSync(new URL('../src/pages/ABTestsPage.tsx', import.meta.url), 'utf8');
const detail = fs.readFileSync(new URL('../src/pages/ABTestDetailPage.tsx', import.meta.url), 'utf8');

const checks = [
  ['API start requires draft_fingerprint', client.includes('draft_fingerprint: string')],
  ['ABTest exposes start_confirmation_fingerprint', types.includes('start_confirmation_fingerprint: string')],
  ['Wizard sends draft_fingerprint', /draft_fingerprint:\s*(?:test|latest)\.start_confirmation_fingerprint/.test(wizard)],
  ['CPM retry sends fresh fingerprint', wizard.includes('latest.start_confirmation_fingerprint')],
  ['Existing campaign resume sends reviewed snapshot fingerprint', detail.includes('resumeConfirmation?.start_confirmation_fingerprint')],
  ['Draft creation sends the calculated budget', wizard.includes('budget_rub: estimatedBudget')],
  ['Minimum budget is loaded from server config', wizard.includes('api.getABTestConfig()')],
  ['Resume preserves reviewed funding source and forbids new deposit', detail.includes('resumeConfirmation?.funding_source') && detail.includes('auto_deposit: false')],
];
const failed = checks.filter(([, ok]) => !ok);
for (const [name, ok] of checks) console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}`);
if (failed.length) process.exit(1);
