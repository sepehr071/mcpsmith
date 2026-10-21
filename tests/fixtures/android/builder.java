package demo.net;

/* Hand-written test fixture in the shape of a request-builder class with renamed annotation classes; not real app code. */
final class RequestBuilderDemo {
    static final String DECOY = "DELETE"; // a verb literal outside any instanceof block says nothing

    void method(Annotation annotation2) {
        if (annotation2 instanceof z9.b) {
            line("DELETE", false);
        } else if (annotation2 instanceof z9.f) {
            line("GET", false);
        } else if (annotation2 instanceof z9.o) {
            line("POST", true);
        } else if (annotation2 instanceof z9.p) {
            line("PUT", true);
        } else if (annotation2 instanceof z9.h) {
            line(h.method(), h.path());
        } else if (annotation2 instanceof z9.k) {
            throw error("@Headers annotation is empty.");
        } else if (!(annotation2 instanceof z9.e)) {
            return;
        }
        String notABlock = "POST";
    }

    void parameter(Annotation annotation, Type type) {
        if (annotation instanceof z9.y) {
            throw error("@Path parameters may not be used with @Url.");
            throw error("@Url must be String or URI.");
            throw error("@Url cannot be used with @%s URL");
        } else {
            boolean isPath = annotation instanceof z9.s;
            if (isPath) {
                if (k.q) {
                    throw error("A @Path parameter must not come after a @Query.");
                }
                if (k.r) {
                    throw error("A @Path parameter must not come after a @QueryName.");
                }
                throw error("@Path parameter name must match %s.");
            } else if (annotation instanceof z9.t) {
                k.q = true;
                handle();
            } else if (annotation instanceof z9.v) {
                k.r = true;
                handle();
            } else if (annotation instanceof z9.u) {
                throw error("@QueryMap parameter type must be Map.");
            } else if (annotation instanceof z9.i) {
                String name = ((z9.i) annotation).value();
                new Handler(name, ((z9.i) annotation).allowUnsafeNonAsciiValues());
            } else if (annotation instanceof z9.a) {
                throw error("@Body parameters cannot be used with form or multi-part encoding.");
            } else if (annotation instanceof z9.c) {
                throw error("@Field parameters can only be used with form encoding.");
            } else if (type instanceof ParameterizedType) {
                throw error("@Query is mentioned here but the type is not an annotation");
            }
        }
    }
}
