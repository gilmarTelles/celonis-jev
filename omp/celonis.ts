/**
 * Celonis Navigator as an omp extension.
 *
 * Registers four tools against the celonis-jev CLI, so any omp session - in any
 * repo - can resolve a phrase to a Celonis location, open it, read a KPI, or
 * report what is missing. The CLI holds the logic; this is the thin surface.
 *
 * Install:   python3 celonis_agent_setup.py        (symlink + wire MCP + doctor)
 * Manual:    ln -s "$PWD/omp/celonis.ts" ~/.omp/agent/extensions/celonis.ts
 * Configure: CELONIS_JEV_HOME=/path/to/celonis-jev when the clone lives elsewhere
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

/** Structural view of the omp extension surface this file touches. */
type Pi = {
	zod: {
		string: () => { optional: () => unknown };
		object: (shape: Record<string, unknown>) => unknown;
	};
	registerTool: (definition: Record<string, unknown>) => void;
	registerCommand: (name: string, definition: Record<string, unknown>) => void;
	on: (event: string, handler: (...args: unknown[]) => void) => void;
	setLabel: (label: string) => void;
	logger?: { debug?: (message: string) => void };
};

type ToolResult = { content: { type: "text"; text: string }[]; details: Record<string, unknown> };

const CANDIDATES: string[] = [
	process.env.CELONIS_JEV_HOME,
	join(homedir(), "celonis-jev"),
	join(homedir(), "Documents/celonis-jev"),
].filter((dir): dir is string => Boolean(dir));

function home(): string | undefined {
	return CANDIDATES.find((dir) => existsSync(join(dir, "celonis_cli.py")));
}

function run(args: string[], timeoutMs = 180_000): string {
	const dir = home();
	if (!dir) {
		throw new Error(
			`celonis-jev not found (looked in ${CANDIDATES.join(", ")}). ` +
				`Set CELONIS_JEV_HOME to the clone that contains celonis_cli.py.`,
		);
	}
	return execFileSync("python3", [join(dir, "celonis_cli.py"), ...args], {
		cwd: dir,
		encoding: "utf8",
		timeout: timeoutMs,
		maxBuffer: 8 * 1024 * 1024,
		env: { ...process.env, PYTHONUNBUFFERED: "1" },
	}).trim();
}

const text = (body: string): ToolResult => ({
	content: [{ type: "text" as const, text: body }],
	details: {},
});

export default function celonisExtension(pi: Pi): void {
	const onePhrase = pi.zod.object({
		phrase: pi.zod.string(),
	});

	pi.setLabel("Celonis Navigator");

	pi.registerTool({
		name: "celonis_resolve",
		label: "Resolve in Celonis",
		description:
			"Resolve a plain-language request to a Celonis location (space, package, asset, object, " +
			"event, KPI or app page): returns the target, a ready deep link, a confidence and the " +
			"runner-up candidates. Read-only, no browser needed.",
		parameters: onePhrase,
		async execute(_id: string, params: { phrase: string }): Promise<ToolResult> {
			return text(run(["resolve", "--json", params.phrase]));
		},
	});

	pi.registerTool({
		name: "celonis_open",
		label: "Open in Celonis",
		description:
			"Resolve a phrase and navigate the signed-in Celonis browser tab to it. Returns the URL " +
			"either way, so it still helps when no browser is attached.",
		parameters: onePhrase,
		async execute(_id: string, params: { phrase: string }): Promise<ToolResult> {
			return text(run(["ask", params.phrase]));
		},
	});

	pi.registerTool({
		name: "celonis_read",
		label: "Read from Celonis",
		description:
			"Answer a data question: resolve the phrase to a KPI, run its PQL through the signed-in " +
			"session, and return the resulting rows.",
		parameters: pi.zod.object({
			phrase: pi.zod.string(),
			pql: pi.zod.string().optional(),
		}),
		async execute(_id: string, params: { phrase: string; pql?: string }): Promise<ToolResult> {
			const args = ["read", params.phrase];
			if (params.pql) args.push(params.pql);
			return text(run(args));
		},
	});

	pi.registerTool({
		name: "celonis_doctor",
		label: "Celonis environment check",
		description:
			"Check credentials, tenant reachability, index age and the signed-in browser. Call this " +
			"first when a celonis_* tool fails.",
		parameters: pi.zod.object({}),
		async execute(): Promise<ToolResult> {
			return text(run(["doctor"]));
		},
	});

	pi.registerCommand("celonis", {
		description: "Ask for a place in Celonis: /celonis the tax projection view",
		handler: async (args: string, ctx: { ui: { notify: (text: string, level: string) => void } }) => {
			const asked = (args || "").trim();
			if (!asked) {
				ctx.ui.notify("usage: /celonis <what you want to see>", "info");
				return;
			}
			try {
				ctx.ui.notify(run(["ask", asked]), "info");
			} catch (error: unknown) {
				const message = error instanceof Error ? error.message : String(error);
				ctx.ui.notify(`celonis: ${message}`, "error");
			}
		},
	});

	pi.on("session_start", () => {
		const dir = home();
		if (!dir) return; // stay invisible unless the CLI is reachable
		pi.logger?.debug?.(`celonis extension active (${dir})`);
	});
}
