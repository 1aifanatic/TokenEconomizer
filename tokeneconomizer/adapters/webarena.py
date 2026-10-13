"""WebArenaAdapter: STUB marking exactly what a WebArena agentic eval must supply.

WebArena / BrowserGym tasks are browser-interaction episodes: the agent emits actions like
`click`, `type`, `goto`, `scroll`, `select_option` against a live web app, and success is
decided by WebArena's own per-task checker (URL match / element / DB state), NOT a gold
patch. Because the economizer's three mechanisms are domain-neutral (they count
turns/tokens over a normalized trajectory), they transfer to WebArena UNCHANGED once this
adapter is implemented. Every method below raises NotImplementedError with the precise
contract it must fulfill.

Implementation notes (for whoever fills this in):

  load_trajectory(path)
      Parse a saved BrowserGym/WebArena episode into the SAME normalized shape the
      economizer expects: [(action_name, args_dict, observation_text, success_bool)]. Map
      browser actions to `action_name` ("click"/"type"/"goto"); put the target
      selector/url/text in args; the resulting page observation (accessibility tree / DOM
      excerpt) is result_text; success is whether the action executed. Order-preserving.

  tool_usage(path)         {action_name: count} over the episode.
  parse_catalog(...)       {action_name: description} of the browser action space (often a
                           fixed constant map -- return it so evidence-untool can reason
                           about unused actions).
  detect(path)             web waste families (repeated navigation, redundant observation,
                           failed selectors) in the shape reflect.detect emits.
  protected_tools()        the browser work surface (click/type/goto/scroll/...); never drop.
  inapplicable_tools()     actions inapplicable-by-construction for a sealed episode.

Also required, OUTSIDE this adapter -- at the runner layer: a web executor that drives one
WebArena episode in its browser env, and WebArena's success checker in place of the gold
grade (see tokeneconomizer.runner.AgentRunner).
"""

_TODO = ("WebArenaAdapter is a stub: implement the WebArena/BrowserGym {what} "
         "(see the module docstring).")


class WebArenaAdapter:
    name = "webarena"

    def load_trajectory(self, path):
        raise NotImplementedError(_TODO.format(what="episode -> normalized trajectory parser"))

    def tool_usage(self, path):
        raise NotImplementedError(_TODO.format(what="per-action usage counter"))

    def parse_catalog(self, run_dir_path, agent):
        raise NotImplementedError(_TODO.format(what="browser action-space catalog"))

    def detect(self, path):
        raise NotImplementedError(_TODO.format(what="web waste-signal detector"))

    def protected_tools(self):
        raise NotImplementedError(_TODO.format(what="browser work-surface protected set"))

    def inapplicable_tools(self):
        raise NotImplementedError(_TODO.format(what="inapplicable-action set"))
