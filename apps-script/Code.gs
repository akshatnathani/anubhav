/**
 * Mailer for the tracker. The server decides WHAT to send (login emails, and reminders
 * only for tasks not logged yet); this script just sends it from your Gmail.
 *
 * Setup:
 *   1. Project Settings > Script Properties:
 *        SERVER_URL  = https://your-tracker-url   (same as BASE_URL in .env, no trailing slash)
 *        MAIL_SECRET = same value as MAIL_SECRET in .env
 *   2. Run setup() once and allow permissions (creates a trigger every 10 minutes).
 *   3. Run checkServer() to confirm it can reach the server.
 */

function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'deliver') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('deliver').timeBased().everyMinutes(10).create();
  Logger.log('Trigger created: deliver() runs every 10 minutes.');
  checkServer();
}

/** Fetch queued emails from the server, send them, and tell the server which were sent. */
function deliver() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return; // previous run still going
  try {
    const res = call_('get', '/api/mail/outbox');
    const messages = res.messages || [];
    if (!messages.length) return;

    const sent = [];
    messages.forEach(function (m) {
      if (MailApp.getRemainingDailyQuota() < 1) {
        console.warn('Gmail daily quota used up; the rest will go out tomorrow.');
        return;
      }
      try {
        MailApp.sendEmail({
          to: m.to,
          subject: m.subject,
          body: m.body + (m.link ? '\n\nOpen: ' + m.link : ''),
          htmlBody: html_(m.body, m.link),
          name: props_().SENDER_NAME || 'Daily Tracker',
        });
        sent.push(m.id);
      } catch (err) {
        console.error('Could not send #' + m.id + ' to ' + m.to + ': ' + err);
      }
    });

    if (sent.length) call_('post', '/api/mail/ack', { ids: sent });
    console.log('Sent ' + sent.length + '/' + messages.length + ' email(s).');
  } finally {
    lock.releaseLock();
  }
}

/** Run by hand: checks the URL and secret without sending anything. */
function checkServer() {
  const p = props_();
  const health = UrlFetchApp.fetch(p.SERVER_URL + '/healthz', { muteHttpExceptions: true });
  Logger.log('GET /healthz -> ' + health.getResponseCode() + ' ' + health.getContentText().slice(0, 80));
  const auth = UrlFetchApp.fetch(p.SERVER_URL + '/api/mail/ack', {
    method: 'post', contentType: 'application/json', payload: '{"ids":[]}',
    headers: { 'X-Mail-Secret': p.MAIL_SECRET }, muteHttpExceptions: true,
  });
  Logger.log('Secret check -> ' + (auth.getResponseCode() === 200 ? 'OK' : 'FAILED (' + auth.getResponseCode() + ')'));
}

function props_() {
  const p = PropertiesService.getScriptProperties().getProperties();
  if (!p.SERVER_URL || !p.MAIL_SECRET) throw new Error('Set SERVER_URL and MAIL_SECRET in Project Settings > Script Properties.');
  p.SERVER_URL = p.SERVER_URL.replace(/\/+$/, '');
  return p;
}

function call_(method, path, body) {
  const p = props_();
  const options = { method: method, headers: { 'X-Mail-Secret': p.MAIL_SECRET }, muteHttpExceptions: true };
  if (body) {
    options.contentType = 'application/json';
    options.payload = JSON.stringify(body);
  }
  const res = UrlFetchApp.fetch(p.SERVER_URL + path, options);
  if (res.getResponseCode() !== 200) {
    throw new Error(method.toUpperCase() + ' ' + path + ' -> HTTP ' + res.getResponseCode() + ': ' + res.getContentText().slice(0, 200));
  }
  return JSON.parse(res.getContentText());
}

function html_(text, link) {
  const esc = function (s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  };
  const button = link
    ? '<p style="margin:22px 0 0"><a href="' + esc(link) + '" style="background:#3056d3;color:#ffffff;' +
      'padding:11px 20px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block">Open tracker</a></p>'
    : '';
  return '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;line-height:1.55;color:#1c1b19;max-width:520px">' +
    '<div style="white-space:pre-wrap">' + esc(text) + '</div>' + button + '</div>';
}
