package com.example.demo.network;

import java.util.List;
import java.util.Map;
import retrofit2.Call;
import retrofit2.http.*;

/* Hand-written test fixture in the shape of decompiled Retrofit interfaces; not real app code. */
public interface DemoApi {
    // request lines and parameter kinds
    @GET("api/v1/timelines/home")
    Call<List<Status>> homeTimeline(@Query("max_id") String maxId, @Query("limit") Integer limit, @Query("count") int count);

    @GET("api/v1/statuses/{id}")
    Call<Status> status(@Path("id") String id);

    @GET("api/v1/search")
    Call<Results> search(@Query("q") String q, @QueryMap Map<String, String> filters, @Header("X-Client") String client);

    @GET("api/v1/search")
    Call<Results> searchByTag(@Query("tag") String tag);

    @FormUrlEncoded
    @POST("api/v1/statuses")
    Call<Status> post(@Field("status") String status, @Field("sensitive") boolean sensitive, @FieldMap Map<String, String> extra);

    @Multipart
    @POST("api/v2/media")
    Call<Media> upload(@Part MultipartBody.Part file, @PartMap Map<String, RequestBody> meta);

    @PUT("api/v1/accounts/{id}")
    Call<Account> update(@Path("id") String id, @Body AccountUpdate update);

    @PATCH("api/v1/accounts/{id}/note")
    Call<Account> note(@Path("id") String id, @Field("comment") String comment);

    @HEAD("api/v1/ping")
    Call<Void> ping();

    @OPTIONS("api/v1/ping")
    Call<Void> options();

    @GET
    Call<ResponseBody> download(@Url String url);

    @HTTP(method = "DELETE", path = "api/v1/lists/{id}/accounts", hasBody = true)
    Call<Void> removeFromList(@Path("id") String id, @Body AccountIds ids);

    // a credential written into the app must never reach the Source
    @Headers({"Authorization: Bearer hunter2secret", "Accept: application/json", "X-Api-Key: abcd1234efgh5678"})
    @GET("api/v1/me")
    Call<Account> me();

    // path built by concatenation
    @GET("users/" + Consts.ID)
    Call<Account> userById();

    @GET(Consts.PATH)
    Call<Account> byConstant();

    // minified annotation names: structure only
    @a("api/v1/minified/{id}")
    Call<Thing> minified(@t("id") String id, @u("limit") int limit);

    @b(method = "PURGE", path = "api/v1/things/{id}", hasBody = false)
    Call<Void> minifiedHttp(@t("id") String id);
}
