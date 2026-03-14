/**
 * Sanitize HTML string — strips dangerous tags and attributes
 * that could lead to XSS (script, iframe, onerror, onclick, etc.)
 */
const DANGEROUS_TAGS = /(<\s*\/?\s*(script|iframe|object|embed|form|input|textarea|select|button|link|meta|base|applet)\b[^>]*>)/gi;
const EVENT_ATTRS = /\s+on\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi;
const JAVASCRIPT_URLS = /\b(href|src|action)\s*=\s*["']?\s*javascript\s*:/gi;
const DATA_URLS = /\b(href|src)\s*=\s*["']?\s*data\s*:\s*text\/html/gi;

export function sanitizeHtml(html) {
  if (!html || typeof html !== 'string') return '';
  return html
    .replace(DANGEROUS_TAGS, '')
    .replace(EVENT_ATTRS, '')
    .replace(JAVASCRIPT_URLS, '$1=""')
    .replace(DATA_URLS, '$1=""');
}
