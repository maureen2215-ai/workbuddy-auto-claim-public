#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>

static NSString *Mode;
static NSString *RequestID;
static NSString *Directory;

static void Record(NSString *status, NSDictionary *extra) {
    NSMutableDictionary *row = [@{@"status":status, @"request_id":RequestID ?: @"",
        @"time":[[NSISO8601DateFormatter new] stringFromDate:[NSDate date]]} mutableCopy];
    [row addEntriesFromDictionary:extra ?: @{}];
    NSData *data=[NSJSONSerialization dataWithJSONObject:row options:0 error:nil];
    [data writeToFile:[Directory stringByAppendingPathComponent:@"notification-result.json"] options:NSDataWritingAtomic error:nil];
    [[NSFileManager defaultManager] setAttributes:@{NSFilePosixPermissions:@0600}
        ofItemAtPath:[Directory stringByAppendingPathComponent:@"notification-result.json"] error:nil];
}
static void Finish(void) {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW,NSEC_PER_SEC),dispatch_get_main_queue(),^{ [NSApp terminate:nil]; });
}

@interface Reminder : NSObject <NSApplicationDelegate,UNUserNotificationCenterDelegate>
@end
@implementation Reminder
- (void)sendNotification:(BOOL)test {
    UNMutableNotificationContent *c=[UNMutableNotificationContent new];
    c.title=test ? @"WorkBuddy 领取提醒 · 测试" : @"WorkBuddy 需要重新登录";
    c.body=test ? @"这是一条修复后的测试通知，不代表领取失败。以后登录、兼容或持续网络故障会在这里提醒你。点击可打开 WorkBuddy。" :
        @"自动领取遇到登录失效。请点击此通知打开 WorkBuddy，重新登录后程序会继续检查。";
    if ([Mode isEqual:@"--attention"]) {
        c.title=@"WorkBuddy 自动领取未完成";
        c.body=@"后台领取遇到持续故障或兼容问题，请打开 WorkBuddy 检查，并让维护本项目的 AI 查看故障记录。必要时先手动领取；点击可打开 WorkBuddy。";
    }
    UNNotificationRequest *request=[UNNotificationRequest requestWithIdentifier:test ? @"test" : @"login-required" content:c trigger:nil];
    [[UNUserNotificationCenter currentNotificationCenter] addNotificationRequest:request withCompletionHandler:^(NSError *error){
        Record(error ? @"SEND_FAILED" : @"QUEUED",error ? @{@"error_code":@(error.code)} : nil);
        Finish();
    }];
}
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    UNUserNotificationCenter *center=[UNUserNotificationCenter currentNotificationCenter];
    if([Mode isEqual:@"--clear"]) {
        [center removeDeliveredNotificationsWithIdentifiers:@[@"login-required"]];
        [center removePendingNotificationRequestsWithIdentifiers:@[@"login-required"]];
        Record(@"CLEARED",nil); Finish(); return;
    }
    if(!Mode) { // A notification click can relaunch the application without arguments.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,10*NSEC_PER_SEC),dispatch_get_main_queue(),^{ [NSApp terminate:nil]; });
        return;
    }
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings){
        if([Mode isEqual:@"--status"]) {
            Record(@"SETTINGS",@{@"authorization":@(settings.authorizationStatus),@"alerts":@(settings.alertSetting)}); Finish(); return;
        }
        if([Mode isEqual:@"--setup"] && settings.authorizationStatus==UNAuthorizationStatusNotDetermined) {
            [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert completionHandler:^(BOOL granted,NSError *error){
                Record(granted ? @"AUTHORIZED" : @"NOT_AUTHORIZED",nil);
                if(granted) [self sendNotification:YES]; else Finish();
            }];
            return;
        }
        if(settings.authorizationStatus!=UNAuthorizationStatusAuthorized || settings.alertSetting!=UNNotificationSettingEnabled) {
            Record(@"NOT_AUTHORIZED",@{@"authorization":@(settings.authorizationStatus),@"alerts":@(settings.alertSetting)}); Finish(); return;
        }
        [self sendNotification:!([Mode isEqual:@"--login"] || [Mode isEqual:@"--attention"])];
    }];
}
- (void)userNotificationCenter:(UNUserNotificationCenter *)center willPresentNotification:(UNNotification *)notification
        withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    completionHandler(UNNotificationPresentationOptionBanner|UNNotificationPresentationOptionList);
}
- (void)userNotificationCenter:(UNUserNotificationCenter *)center didReceiveNotificationResponse:(UNNotificationResponse *)response
        withCompletionHandler:(void (^)(void))completionHandler {
    if([response.actionIdentifier isEqual:UNNotificationDefaultActionIdentifier]) {
        NSWorkspaceOpenConfiguration *config=[NSWorkspaceOpenConfiguration configuration]; config.activates=YES;
        [[NSWorkspace sharedWorkspace] openApplicationAtURL:[NSURL fileURLWithPath:@"/Applications/WorkBuddy.app"]
            configuration:config completionHandler:^(NSRunningApplication *app,NSError *error){
                Record(app ? @"WORKBUDDY_OPENED" : @"WORKBUDDY_OPEN_FAILED",nil); Finish();
            }];
    } else Finish();
    completionHandler();
}
@end

int main(int argc,const char **argv) {
    @autoreleasepool {
        Directory=[NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/WorkBuddyAutoClaim"];
        [[NSFileManager defaultManager] createDirectoryAtPath:Directory withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:nil];
        Mode=argc>1 ? @(argv[1]) : nil;
        RequestID=argc>2 ? @(argv[2]) : @"";
        if(Mode && ![@[@"--setup",@"--status",@"--test",@"--login",@"--attention",@"--clear"] containsObject:Mode]) return 2;
        [NSApplication sharedApplication];
        Reminder *delegate=[Reminder new];
        NSApp.delegate=delegate;
        [UNUserNotificationCenter currentNotificationCenter].delegate=delegate;
        // No credential access, network calls, accessibility permission or remote notifications.
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW,120*NSEC_PER_SEC),dispatch_get_main_queue(),^{ [NSApp terminate:nil]; });
        [NSApp run];
    }
    return 0;
}
