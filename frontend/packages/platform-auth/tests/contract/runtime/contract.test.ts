import { expectTypeOf, it } from "vitest";

import type {
    AgentCatalogItem,
    AgentCatalogSettings,
    AgentRegistrationDetails,
    UserPolicy,
} from "../../../src";

it("pins nullable/optional Entry administration DTO fields", () => {
    expectTypeOf<AgentCatalogSettings["management_url"]>().toEqualTypeOf<
        string | null
    >();
    expectTypeOf<UserPolicy["manageable_agent_keys"]>().toEqualTypeOf<
        string[] | null | undefined
    >();
    expectTypeOf<AgentCatalogItem["entry_url"]>().toEqualTypeOf<
        string | null
    >();
    expectTypeOf<AgentCatalogItem["management_url"]>().toEqualTypeOf<
        string | null | undefined
    >();
    expectTypeOf<AgentRegistrationDetails>().toEqualTypeOf<{
        slug: string;
        entry_url: string;
        management_url: string;
        backend_url: string;
    }>();
});
