/**
 * Mailer for the tracker, deployed as a web app.
 * The Worker POSTs {to, subject, body, link} and this sends it from your Gmail.
 *
 * Deploy > New deployment > Web app
 *   Execute as:     Me
 *   Who has access: Anyone
 * Put the Web app URL in the Worker secret APPS_SCRIPT_URL.
 * Keep that URL private: anyone who has it can send email from your account.
 */

const SENDER_NAME = "Anubhav's You Lil Bitch";

function doPost(e) {
  try {
    const m = JSON.parse(e.postData.contents);
    if (!m.to || !m.subject || !m.body) return json_({ ok: false, error: 'to, subject and body are required' });
    MailApp.sendEmail({
      to: m.to,
      subject: m.subject,
      body: m.body + (m.link ? '\n\nOpen: ' + m.link : ''),
      htmlBody: html_(m.body, m.link),
      name: SENDER_NAME,
    });
    return json_({ ok: true, quotaLeft: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

/** Open the web app URL in a browser to check it's deployed. */
function doGet() {
  return json_({ ok: true, service: 'tracker-mailer', quotaLeft: MailApp.getRemainingDailyQuota() });
}

/** Run once in the editor to grant the "send email" permission before deploying. */
function authorize() {
  Logger.log('Emails left today: ' + MailApp.getRemainingDailyQuota());
}

/**
 * Backup timer for the reminders, because Cloudflare's own cron triggers are unreliable
 * on Free-plan accounts (registered but never dispatched).
 *
 * Project Settings > Script Properties:  JOBS_URL = https://<your worker>/api/run-jobs?key=<JOBS_SECRET>
 * Then run setupTrigger() once.
 */
function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'runJobs') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('runJobs').timeBased().everyMinutes(5).create();
  Logger.log('Trigger created: runJobs() runs every 5 minutes.');
  runJobs();
}

function runJobs() {
  const url = PropertiesService.getScriptProperties().getProperty('JOBS_URL');
  if (!url) throw new Error('Set JOBS_URL in Project Settings > Script Properties.');
  const res = UrlFetchApp.fetch(url, { method: 'post', muteHttpExceptions: true });
  Logger.log('run-jobs -> ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 200));
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
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
