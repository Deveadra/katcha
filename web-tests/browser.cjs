const playwright = require("playwright");
const name = process.env.KATCHA_TEST_BROWSER || "chromium";
if (!["chromium", "firefox", "webkit"].includes(name)) {
    throw new Error("KATCHA_TEST_BROWSER must be chromium, firefox, or webkit");
}
module.exports = {
    launch: () => playwright[name].launch({
        headless: true,
        ...(name === "chromium" && process.env.CHROMIUM_PATH ? {
            executablePath: process.env.CHROMIUM_PATH,
            args: ["--no-sandbox"],
        } : {}),
    }),
};
