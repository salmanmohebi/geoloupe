// Failures of the browser tests as GitHub Actions annotations: they are listed on the run's page and
// readable through the public API (check-run annotations), also without access to the logs.

const onGitHub = !!process.env.GITHUB_ACTIONS;

/** Print one failure as an annotation (on GitHub Actions only). */
export function annotate(test, message) {
  if (!onGitHub) return;
  const text = String(message).replace(/%/g, "%25").replace(/\r/g, "").replace(/\n/g, "%0A");
  console.log(`::error title=${test}::${text.slice(0, 1500)}`);
}

/** Annotate a crash (an uncaught error, e.g. a Playwright timeout) before the process ends. */
export function reportCrashes(test) {
  const crash = (e) => {
    annotate(test, e?.stack ?? e);
    console.error(e);
    process.exit(1);
  };
  process.on("uncaughtException", crash);
  process.on("unhandledRejection", crash);
}
